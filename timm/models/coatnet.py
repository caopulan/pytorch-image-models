"""VGG

Adapted from https://github.com/pytorch/vision 'vgg.py' (BSD-3-Clause) with a few changes for
timm functionality.

Copyright 2021 Ross Wightman
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Union, List, Dict, Any, cast, Tuple

from .layers import DropBlock2d, DropPath, AvgPool2dSame, BlurPool2d, create_attn, get_attn, create_classifier
from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD, CIFAR_DEFAULT_MEAN, CIFAR_DEFAULT_STD
from .helpers import build_model_with_cfg
from .layers import ClassifierHead, ConvBnAct
from .registry import register_model
from einops import rearrange
from einops.layers.torch import Rearrange

__all__ = [
    'CoAtNet', 'coatnet_0'
]


def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 1000, 'input_size': (3, 224, 224), 'pool_size': (1, 1),
        'crop_pct': 0.9, 'interpolation': 'bicubic',
        'mean': CIFAR_DEFAULT_MEAN, 'std': CIFAR_DEFAULT_STD,
        'first_conv': 'features.0', 'classifier': 'head.fc',
        **kwargs
    }
    # return {
    #
    # }


default_cfgs = {
    'coatnet_0': _cfg(url=''),
}


cfgs: Dict[str, List[Union[str, int]]] = {
    'vgg11': [64, 'M', 128, 'M', 256, 256, 'M', 512, 512, 'M', 512, 512, 'M'],
    'vgg13': [64, 64, 'M', 128, 128, 'M', 256, 256, 'M', 512, 512, 'M', 512, 512, 'M'],
    'vgg16': [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 'M', 512, 512, 512, 'M', 512, 512, 512, 'M'],
    'vgg19': [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 256, 'M', 512, 512, 512, 512, 'M', 512, 512, 512, 512, 'M'],
}

class SeConv2d(nn.Module):
    def __init__(self,
                 inplanes: int,
                 innerplanse: int,
                 inner_act: str = 'GELU',
                 out_act: str = 'Sigmoid') -> None:
        super(SeConv2d, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.conv = nn.Sequential(
            nn.Conv2d(inplanes, innerplanse, 1),
            nn.GELU(),
            nn.Conv2d(innerplanse, inplanes, 1),
            nn.Sigmoid()
        )
    #     self._init_weights()
    #
    # def _init_weights(self) -> None:
    #     for m in self.modules():
    #         if isinstance(m, nn.Conv2d):
    #             nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity="GELU")
    #             if m.bias is not None:
    #                 nn.init.zeros_(m.bias)

    def forward(self, x):
        y = self.avg_pool(x)
        y = self.conv(y)
        return x * y


class MBConv(nn.Module):
    def __init__(self,
                 inplanes: int,
                 outplanes: int,
                 stride: int = 1,
                 kernel: int = 3,
                 dilation: int = 1,
                 groups: Tuple[int, int] = (1, 1),
                 t: int = 4.0,
                 norm: str = 'BN',
                 bn_eps: float = 1e-5,
                 act: str = 'GELU6',
                 se_ratio: float = 0.25,
                 **kwargs) -> None:
        super(MBConv, self).__init__()

        self.stride = stride
        if stride > 1:
            self.pool = nn.MaxPool2d(3, stride, 1)
            self.proj = nn.Conv2d(inplanes, outplanes, 1)
        padding = (dilation * kernel - dilation) // 2
        self.inplanes, self.outplanes = int(inplanes), int(outplanes)
        innerplanes = int(inplanes * t)
        self.t = t

        self.conv1 = nn.Conv2d(self.inplanes, innerplanes, 1, stride=stride, padding=0, groups=groups[0], bias=False)
        self.bn1 = nn.BatchNorm2d(innerplanes, eps=bn_eps)
        self.conv2 = nn.Conv2d(innerplanes, innerplanes, kernel, stride=1, padding=padding,
                               dilation=dilation, groups=innerplanes, bias=False)
        self.bn2 = nn.BatchNorm2d(innerplanes, eps=bn_eps)

        # se_base_chs = innerplanes if se_reduce_mid else self.inplanes
        # se_innerplanse = int(se_base_chs * se_ratio)
        se_innerplanse = int(self.inplanes * se_ratio)
        if se_ratio:
            self.se = SeConv2d(innerplanes, se_innerplanse)
        else:
            self.se = None
        self.conv3 = nn.Conv2d(innerplanes, self.outplanes, 1, stride=1, padding=0, groups=groups[1], bias=False)
        self.bn3 = nn.BatchNorm2d(self.outplanes, eps=bn_eps)

        self.act = nn.GELU()

    def forward(self, x):
        if self.stride > 1:
            residual = self.proj(self.pool(x))
        else:
            residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.act(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.act(out)
        if self.se is not None:
            out = self.se(out)

        out = self.conv3(out)
        out = self.bn3(out)

        out = out + residual

        return out


class Conv_Block(nn.Module):

    def __init__(self,
                 l: int, # repeat times
                 inplanes: int,
                 outplanes: int,
                 width,
                 height,
                 idx,
                 **kwargs):
        super(Conv_Block, self).__init__()

        blocks = []
        for i in range(l):
            if i == 0:
                if idx == 3:
                    stirde = 4
                else:
                    stride = 2
            else:
                stride = 1
            in_dim = inplanes if i == 0 else outplanes
            blocks.append(MBConv(inplanes=in_dim, outplanes=outplanes, stride=stride))
        self.block = nn.Sequential(*blocks)

    def forward(self, x):
        return self.block(x)


class PreNorm(nn.Module):
    def __init__(self, dim, fn, norm):
        super().__init__()
        self.norm = norm(dim)
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)

class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, dropout=0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, dim),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        return self.net(x)


class Attention(nn.Module):
    def __init__(self, inp, oup, image_size, dim_head=32, dropout=0.):
        super().__init__()
        heads = oup // dim_head
        # inner_dim = dim_head * heads
        inner_dim = oup
        assert inner_dim == oup, f"heads {heads}, dim_head {dim_head}, oup {oup}"

        self.ih, self.iw = image_size

        self.heads = heads
        self.scale = dim_head ** -0.5

        # parameter table of relative position bias
        self.relative_bias_table = nn.Parameter(
            torch.zeros((2 * self.ih - 1) * (2 * self.iw - 1), heads))

        coords = torch.meshgrid((torch.arange(self.ih), torch.arange(self.iw)))
        coords = torch.flatten(torch.stack(coords), 1)
        relative_coords = coords[:, :, None] - coords[:, None, :]

        relative_coords[0] += self.ih - 1
        relative_coords[1] += self.iw - 1
        relative_coords[0] *= 2 * self.iw - 1
        relative_coords = rearrange(relative_coords, 'c h w -> h w c')
        relative_index = relative_coords.sum(-1).flatten().unsqueeze(1)
        self.register_buffer("relative_index", relative_index)

        self.attend = nn.Softmax(dim=-1)
        self.to_qkv = nn.Linear(inp, inner_dim * 3, bias=False)

        self.to_out = nn.Sequential(
            nn.Linear(oup, oup),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        qkv = self.to_qkv(x).chunk(3, dim=-1)
        q, k, v = map(lambda t: rearrange(
            t, 'b n (h d) -> b h n d', h=self.heads), qkv)

        dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale

        # Use "gather" for more efficiency on GPUs
        relative_bias = self.relative_bias_table.gather(
            0, self.relative_index.repeat(1, self.heads))
        relative_bias = rearrange(
            relative_bias, '(h w) c -> 1 c h w', h=self.ih*self.iw, w=self.ih*self.iw)
        dots = dots + relative_bias

        attn = self.attend(dots)
        out = torch.matmul(attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        out = self.to_out(out)
        return out



class Transformer(nn.Module):
    def __init__(self, inp, oup, image_size, idx, stride, drop_path=0., heads=8, dim_head=32, downsample=False, dropout=0.):
        super().__init__()
        hidden_dim = int(oup * 4)

        self.ih, self.iw = image_size
        self.downsample = downsample

        if self.downsample:
            self.stride = stride
            self.pool1 = nn.MaxPool2d(3, self.stride, 1)
            self.pool2 = nn.MaxPool2d(3, self.stride, 1)
            self.proj = nn.Conv2d(inp, oup, 1, 1, 0, bias=False)

        self.attn = Attention(inp, oup, image_size, dim_head, dropout)
        self.ff = FeedForward(oup, hidden_dim, dropout)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()

        self.attn = nn.Sequential(
            Rearrange('b c ih iw -> b (ih iw) c'),
            PreNorm(inp, self.attn, nn.LayerNorm),
            Rearrange('b (ih iw) c -> b c ih iw', ih=self.ih, iw=self.iw)
        )

        self.ff = nn.Sequential(
            Rearrange('b c ih iw -> b (ih iw) c'),
            PreNorm(oup, self.ff, nn.LayerNorm),
            Rearrange('b (ih iw) c -> b c ih iw', ih=self.ih, iw=self.iw)
        )

    def forward(self, x):
        if self.downsample:
            x = self.proj(self.pool1(x)) + self.drop_path(self.attn(self.pool2(x)))
        else:
            x = x + self.drop_path(self.attn(x))
        x = x + self.drop_path(self.ff(x))
        return x

class Transformer_Block(nn.Module):

    def __init__(self,
                 l: int,  # repeat times
                 inplanes: int,
                 outplanes: int,
                 width,
                 height,
                 drop_path,
                 stride,
                 idx):
        super(Transformer_Block, self).__init__()
        layers = []
        for i in range(l):
            downsample = True if i == 0 else False
            in_dim = inplanes if i == 0 else outplanes
            layers.append(Transformer(in_dim, outplanes, downsample=downsample, image_size=(width, height), drop_path=drop_path, idx=idx, stride=stride))
        self.block = nn.Sequential(*layers)

    def forward(self, x):
        x = self.block(x)
        return (x)


class Stem(nn.Module):

    def __init__(self, dim):
        super(Stem, self).__init__()
        self.conv1 = nn.Conv2d(3, dim, 3, stride=2, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(dim)
        self.conv2 = nn.Conv2d(dim, dim, 3, bias=False)
        self.bn2 = nn.BatchNorm2d(dim)
        self.act = nn.GELU()

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.act(x)
        x = self.conv2(x)
        x = self.bn2(x)
        x = self.act(x)
        return x

class CoAtNet(nn.Module):

    def __init__(self,
                 # cfg,
                 num_classes,
                 L,
                 dims,
                 input_size,
                 stride,
                 stages: str = 'CCTT',
                 in_chans: int = 3,
                 output_stride: int = 32,
                 drop_rate: float = 0.0,
                 drop_path: float = 0.0,
                 ):
        super(CoAtNet, self).__init__()
        self.num_classes = num_classes
        self.input_size = input_size
        self.stride = stride
        width, height = input_size[1:]
        width = width // stride[0]
        height = height // stride[0]
        self.stage0 = Stem(dims[0])
        for i, (l, d, s0, s) in enumerate(zip(L[1:], dims[1:], stride[1:], list(stages))):
            block = Conv_Block if s == 'C' else Transformer_Block
            width = width // s0
            height = height // s0
            self.add_module(f'stage{i+1}', block(
                inplanes=dims[i], outplanes=dims[i+1], l=l, width=width, height=height,
                drop_path=drop_path, idx=i+1, stride=s0
            ))
        self.global_pool, self.fc = create_classifier(dims[-1], self.num_classes)


    def forward(self, x):
        x = self.stage0(x)
        x = self.stage1(x)
        x = self.stage2(x)
        x = self.stage3(x)
        x = self.stage4(x)
        x = self.global_pool(x)
        x = self.fc(x)
        return x


def _filter_fn(state_dict):
    """ convert patch embedding weight from manual patchify + linear proj to conv"""
    out_dict = {}
    for k, v in state_dict.items():
        k_r = k
        k_r = k_r.replace('classifier.0', 'pre_logits.fc1')
        k_r = k_r.replace('classifier.3', 'pre_logits.fc2')
        k_r = k_r.replace('classifier.6', 'head.fc')
        if 'classifier.0.weight' in k:
            v = v.reshape(-1, 512, 7, 7)
        if 'classifier.3.weight' in k:
            v = v.reshape(-1, 4096, 1, 1)
        out_dict[k_r] = v
    return out_dict


def _create_coatnet(variant: str, pretrained: bool, **kwargs: Any):
    cfg = variant.split('_')[0]
    # NOTE: VGG is one of the only models with stride==1 features, so indices are offset from other models
    out_indices = kwargs.get('out_indices', (0, 1, 2, 3, 4, 5))
    model = build_model_with_cfg(
        CoAtNet, variant, pretrained,
        default_cfg=default_cfgs[variant],
        # model_cfg=cfgs[cfg],
        feature_cfg=dict(flatten_sequential=True, out_indices=out_indices),
        pretrained_filter_fn=_filter_fn,
        **kwargs)
    return model

@register_model
def coatnet_0(pretrained: bool = False, **kwargs: Any):
    model_args = dict(
        stride = [2, 2, 2, 2, 2],
        L = [2, 2, 3, 5, 2],
        dims = [64, 96, 192, 384, 768],
        input_size = [3, 224, 224],
        drop_path = 0.2,
        **kwargs)
    return _create_coatnet('coatnet_0', pretrained=pretrained, **model_args)

@register_model
def coatnet_1(pretrained: bool = False, **kwargs: Any):
    model_args = dict(
        stride = [2, 2, 2, 2, 2],
        L = [2, 2, 6, 14, 2],
        dims = [64, 96, 192, 384, 768],
        input_size = [3, 224, 224],
        drop_path = 0.3,
        **kwargs)
    return _create_coatnet('coatnet_0', pretrained=pretrained, **model_args)

# @register_model
# def vgg11(pretrained: bool = False, **kwargs: Any) -> VGG:
#     r"""VGG 11-layer model (configuration "A") from
#     `"Very Deep Convolutional Networks For Large-Scale Image Recognition" <https://arxiv.org/pdf/1409.1556.pdf>`._
#     """
#     model_args = dict(**kwargs)
#     return _create_vgg('vgg11', pretrained=pretrained, **model_args)
