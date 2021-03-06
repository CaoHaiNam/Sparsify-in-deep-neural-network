import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from torch.distributions import Bernoulli, LogNormal
import numpy as np
from torch.nn.modules.utils import _single, _pair, _triple
from torch import Tensor

from sccl_layer import DynamicLinear, DynamicConv2D, _DynamicLayer, DynamicBatchNorm, DynamicClassifier

import math

import torch.nn as nn
import torch.nn.init as init
from utils import *

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class Normalize(nn.Module):
    """Changes view using a nn.Module."""

    def __init__(self):
        super(Normalize, self).__init__()

    def forward(self, input):
        if self.training:
            return input
        else:
            return input/input.norm(2, dim=-1).view(-1, 1)

class _DynamicModel(nn.Module):
    """docstring for ClassName"""
    def __init__(self):
        super(_DynamicModel, self).__init__()


    def restrict_gradients(self):
        for m in self.DM:
            m.restrict_gradients()

    def expand(self, new_class, max_mul=0.0, max_params=0):
        bound = list(self.bound)

        if max_mul != 0:
            max_bound = list(self.bound*math.sqrt(max_mul))

        add_in = 0
        model_params = self.compute_model_size()[0]
        print('number of neurons added:', end=' ')
        for i, m in enumerate(self.DM[:-1]):
            if max_params > 0:
                # ignore the max number of neurons
                add_out = int(bound[i])
                if isinstance(self.DM[i], DynamicConv2D) and isinstance(self.DM[i+1], DynamicLinear):
                    factor = self.smid * self.smid
                else:
                    factor = 1
                add_params = m.num_add(add_in, add_out) + self.DM[i+1].num_add(add_out*factor, 0)
                if model_params + add_params <= max_params:
                    add_out = int(bound[i])
                else:
                    add_params = max_params - model_params
                    if add_params <= 0:
                        add_out = 0
                    else:
                        a1, b1 = m.add_out_factor(add_in)
                        a2, b2 = self.DM[i+1].add_in_factor(0)
                        # add_params = a1*add_out + b1 +a2*add_out*factor + b2
                        add_out = (add_params - (b1+b2)) / (a1+a2*factor)
                        add_out = int(round(add_out))
                        add_out = max(0, add_out)
                model_params += m.num_add(add_in, add_out)
                # print('add_params_layer {}/{}'.format(m.num_add(add_in, add_out), self.DM[i+1].num_add(add_out*factor, 0)))
                # print('add_params {}/{}'.format(add_params, m.num_add(add_in, add_out)))
            elif max_mul > 0:
                add_out = min(int(bound[i]), int(max_bound[i])-m.out_features)
            else:
                add_out = int(bound[i])

            print(add_out, end=' ')
            self.DM[i].expand(add_in, add_out)

            if isinstance(self.DM[i], DynamicConv2D) and isinstance(self.DM[i+1], DynamicLinear):
                add_in = self.smid * self.smid * add_out
            else:
                add_in = add_out

        self.DM[-1].expand(add_in, new_class)
        print(new_class)


    def squeeze(self, masks):
        mask_in = torch.ones(self.DM[0].in_features).bool().cuda()
        for i, m in enumerate(self.DM[:-1]):
            mask_out = masks[i]
            m.squeeze(mask_in, mask_out)
            if isinstance(m, DynamicConv2D) and isinstance(self.DM[i+1], DynamicLinear):
                mask_in = mask_out.view(-1,1,1).expand(mask_out.size(0),self.smid,self.smid).contiguous().view(-1)
            else:
                mask_in = mask_out

        mask_out = torch.ones(self.DM[-1].out_features).bool().cuda()
        self.DM[-1].squeeze(mask_in, mask_out)

    def forget(self, ratio, max_mul=0.0, max_params=0):
        max_bound = list(self.bound*math.sqrt(max_mul))
        mask_in = torch.ones(self.DM[0].in_features).bool().cuda()
        print('removed:', end=' ')
        for i, m in enumerate(self.DM[:-1]):
            pre = m.out_features
            if m.out_features == int(max_bound[i]) and max_params == 0:
            # if True:
                if isinstance(m, DynamicConv2D):
                    norm = m.weight.norm(2, dim=(1,2,3))
                    if isinstance(self.DM[i+1], DynamicLinear):
                        norm *= self.DM[i+1].weight.view(self.DM[i+1].out_features, m.out_features, self.smid, self.smid).norm(2, dim=(0,2,3))
                        values, indices = norm.sort(descending=False)
                        mask_out = norm > values[int(norm.size(0)*ratio)]
                        m.forget(mask_in, mask_out)
                        mask_in = mask_out.view(-1,1,1).expand(mask_out.size(0),self.smid,self.smid).contiguous().view(-1)
                    else:
                        norm *= self.DM[i+1].weight.norm(2, dim=(0,2,3))
                        values, indices = norm.sort(descending=False)
                        mask_out = norm > values[int(norm.size(0)*ratio)]
                        m.forget(mask_in, mask_out)
                        mask_in = mask_out
                else:
                    norm = m.weight.norm(2, dim=1) * self.DM[i+1].weight.norm(2, dim=0)
                    values, indices = norm.sort(descending=False)
                    mask_out = norm > values[int(norm.size(0)*ratio)]
                    m.forget(mask_in, mask_out)
                    mask_in = mask_out
            else:
                mask_out = torch.ones(m.out_features).bool().cuda()
                m.forget(mask_in, mask_out)
                if isinstance(m, DynamicConv2D) and isinstance(self.DM[i+1], DynamicLinear):
                    mask_in = mask_out.view(-1,1,1).expand(mask_out.size(0),self.smid,self.smid).contiguous().view(-1)
                else:
                    mask_in = mask_out

            print(pre-m.out_features, end=' ')

        print()
        mask_out = torch.ones(self.DM[-1].out_features).bool().cuda()
        self.DM[-1].forget(mask_in, mask_out)

    def group_lasso_reg(self):
        reg = 0
        strength = 0
        for i, m in enumerate(self.DM[:-1]):
            norm_in = m.norm_in()
            bn_norm = m.bn_norm()
            if isinstance(m, DynamicConv2D) and isinstance(self.DM[i+1], DynamicLinear):
                # norm_out = self.DM[i+1].norm_out(size=(self.DM[i+1].shape_out[-1], 
                #                                   m.shape_out[-1]-m.shape_out[-2], 
                #                                   self.smid, self.smid))
                norm_out = self.DM[i+1].norm_out(size=(self.DM[i+1].shape_out[-1], 
                                                    m.shape_out[-1], 
                                                    self.smid, self.smid))
            else:
                norm_out = self.DM[i+1].norm_out()

            reg += norm_in.sum() * m.strength_in
            reg += norm_out.sum() * self.DM[i+1].strength_out
            if m.batch_norm is not None:
                reg += bn_norm.sum() * m.strength_in

            strength += m.strength_in + self.DM[i+1].strength_out
                            
        # reg += self.DM[-1].norm_in().sum() * self.DM[-1].strength_in
        # strength += self.DM[-1].strength_in
        # reg = reg/strength + self.DM[-1].norm_in().sum()
        return reg/strength

    def backward_reg(self):
        reg = 0
        # tasknum = len(self.DM[-1].shape_out)-1
        for i, m in enumerate(self.DM[1:]):
            reg += m.backward_reg(-2)
        return reg

    def new_task(self):
        # save old task
        for m in self.DM:
            m.new_task()

    def forward(self, input, t=-1):
        for module in self.layers:
            if isinstance(module, _DynamicLayer):
                input = module(input, t)
            else:
                input = module(input)

        return input

    def forward_mask(self, input, masks, t):
        i = 0
        for module in self.layers[:-1]:
            if isinstance(module, _DynamicLayer):
                input = module(input, t)
                if masks[i] is not None:
                    if isinstance(module, DynamicLinear):
                        input = input * masks[i]
                    elif isinstance(module, DynamicConv2D):
                        input = input * masks[i].view(1,-1,1,1).expand_as(input)
                i += 1
            else:
                input = module(input)
            # try:
            #   input = module(input, -1)
            #   try:
            #       try:
            #           input = input * masks[i]
            #       except:
            #           input = input * masks[i].view(1,-1,1,1).expand_as(input)
            #   except:
            #       pass
            #   i += 1
            # except:
            #   input = module(input)
        input = self.layers[-1](input, t)
        return input

    def compute_model_size(self, t=-1):
        model_count = 0
        layers_count = []
        for m in self.DM:
            temp_count = 0
            weight = m.weight[:m.shape_out[t]][:, :m.shape_in[t]]
            temp_count += np.prod(weight.size())
            if m.bias is not None:
                bias = m.bias[:m.shape_out[t]]
                temp_count += np.prod(bias.size())
            if m.batch_norm is not None:
                for s in m.shape_out[:t]:
                    temp_count += s*2
                temp_count += m.shape_out[t]*2

            model_count += temp_count
            layers_count.append(temp_count)

        return model_count, layers_count

    def track_gradient(self, sbatch):
        for i, m in enumerate(self.DM[:-1]):

            # m.S -= m.norm_in_grad() * sbatch
            norm_in = m.norm_in_grad()
            if isinstance(m, DynamicConv2D) and isinstance(self.DM[i+1], DynamicLinear):
                norm_out = self.DM[i+1].norm_out_grad(size=(self.DM[i+1].shape_out[-1], 
                                                    m.shape_out[-1], 
                                                    self.smid, self.smid))
            else:
                norm_out = self.DM[i+1].norm_out_grad()

            m.grad_in -= norm_in*sbatch
            m.grad_out -= norm_out*sbatch

    def set_cur_task(self, t):
        mask_in = torch.ones(self.DM[0].in_features).bool().cuda()
        for i, m in enumerate(self.DM[:-1]):
            mask_out = m.mask_pre[t]
            m.cur_weight = m.weight[:m.shape_out[t], :m.shape_in[t]][mask_out][:, mask_in]
            m.cur_bias = m.bias[:m.shape_out[t]][mask_out]
            # print(m.cur_bias)
            if isinstance(m, DynamicConv2D) and isinstance(self.DM[i+1], DynamicLinear):
                mask_in = mask_out.view(-1,1,1).expand(mask_out.size(0),self.smid,self.smid).contiguous().view(-1)
            else:
                mask_in = mask_out

        mask_out = torch.ones(self.DM[-1].out_features).bool().cuda()
        m = self.DM[-1]
        m.cur_weight = m.weight[m.shape_out[t-1]:m.shape_out[t], :m.shape_in[t]][:, mask_in]
        m.cur_bias = m.bias[m.shape_out[t-1]:m.shape_out[t]]


class MLP(_DynamicModel):

    def __init__(self, input_size, taskcla=None, mul=1):
        super(MLP, self).__init__()
        self.mul = mul
        if taskcla is not None:
            output_size = sum([ncla for _, ncla in taskcla])
        else:
            output_size = 0

        self.layers = nn.ModuleList([
            nn.Flatten(),
            nn.Dropout(0.25),
            DynamicLinear(np.prod(input_size), 0),
            nn.ReLU(),
            # nn.Dropout(0.25),
            DynamicLinear(0, 0),
            nn.ReLU(),
            # nn.Dropout(0.25),
            # Normalize(),
            DynamicLinear(0, output_size),
            ])
        
        self.DM = [m for m in self.layers if isinstance(m, _DynamicLayer)]
        self.bound = np.array([400, 400])*self.mul



def compute_conv_output_size(Lin,kernel_size,stride=1,padding=0,dilation=1):
    return int(np.floor((Lin+2*padding-dilation*(kernel_size-1)-1)/float(stride)+1))


class VGG8(_DynamicModel):

    def __init__(self, input_size, taskcla=None, mul=1, batch_norm=False):
        super(VGG8, self).__init__()

        nchannels, size, _ = input_size
        self.mul = mul

        if taskcla is not None:
            output_size = sum([ncla for _, ncla in taskcla])
        else:
            output_size = 0

        self.layers = nn.ModuleList([
            # nn.Dropout(0.25),
            DynamicConv2D(nchannels, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Dropout(0.25),

            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Dropout(0.25),

            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Dropout(0.5),

            nn.Flatten(),
            # nn.Dropout(0.5),
            DynamicLinear(0, 0, batch_norm=batch_norm),
            nn.ReLU(),
            # nn.Dropout(0.5),
            DynamicLinear(0, output_size)
            ])

        self.smid = size
        for m in self.layers:
            if isinstance(m, DynamicConv2D) or isinstance(m, nn.MaxPool2d):
                try:
                    self.smid = compute_conv_output_size(self.smid, m.kernel_size[0], m.stride[0], m.padding[0], m.dilation[0])
                except:
                    self.smid = compute_conv_output_size(self.smid, m.kernel_size, m.stride, m.padding, m.dilation)

        self.DM = [m for m in self.layers if isinstance(m, _DynamicLayer)]
        self.bound = np.array([32, 32, 64, 64, 128, 128, 256])*self.mul


class VGG16(_DynamicModel):

    def __init__(self, input_size, taskcla=None, mul=1, batch_norm=True):
        super(VGG16, self).__init__()
        ncha, size, _ = input_size
        self.mul = mul

        if taskcla is not None:
            output_size = sum([ncla for _, ncla in taskcla])
        else:
            output_size = 0

        self.layers = nn.ModuleList([
            DynamicConv2D(ncha, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            nn.MaxPool2d(2),

            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            nn.MaxPool2d(2),

            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            nn.MaxPool2d(2),

            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            nn.MaxPool2d(2),

            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            DynamicConv2D(0, 0, kernel_size=3, padding=1, batch_norm=batch_norm),
            nn.ReLU(),
            nn.MaxPool2d(2),

            View(),
            DynamicLinear(0, 0),
            nn.ReLU(),
            DynamicLinear(0, 0),
            nn.ReLU(),
            DynamicLinear(0, output_size)
            ])

        self.smid = size
        for m in self.layers:
            if isinstance(m, DynamicConv2D) or isinstance(m, nn.MaxPool2d):
                try:
                    self.smid = compute_conv_output_size(self.smid, m.kernel_size[0], m.stride[0], m.padding[0], m.dilation[0])
                except:
                    self.smid = compute_conv_output_size(self.smid, m.kernel_size, m.stride, m.padding, m.dilation)

        self.DM = [m for m in self.layers if isinstance(m, _DynamicLayer)]
        self.bound = np.array([64, 64  , 128, 128   , 256, 256, 256   , 512, 512, 512   , 512, 512, 512,  4096,  4096])*self.mul


class Alexnet(_DynamicModel):

    def __init__(self, input_size, taskcla=None, mul=1):
        super(Alexnet,self).__init__()

        ncha, size, _ = input_size
        self.mul = mul
        if taskcla is not None:
            output_size = sum([ncla for _, ncla in taskcla])
        else:
            output_size = 0

        self.layers = nn.ModuleList([
            DynamicConv2D(ncha,0,kernel_size=size//8),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Dropout(0.2),

            DynamicConv2D(0,0,kernel_size=size//10),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Dropout(0.2),

            DynamicConv2D(0,0,kernel_size=2),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Dropout(0.5),

            View(),
            DynamicLinear(0, 0),
            nn.ReLU(),
            # nn.Dropout(0.5),
            DynamicLinear(0, 0),
            nn.ReLU(),
            # nn.Dropout(0.5),
            DynamicLinear(0, output_size)
            ])

        self.smid = size
        for m in self.layers:
            if isinstance(m, DynamicConv2D) or isinstance(m, nn.MaxPool2d):
                try:
                    self.smid = compute_conv_output_size(self.smid, m.kernel_size[0], m.stride[0], m.padding[0], m.dilation[0])
                except:
                    self.smid = compute_conv_output_size(self.smid, m.kernel_size, m.stride, m.padding, m.dilation)

        self.DM = [m for m in self.layers if isinstance(m, _DynamicLayer)]
        self.bound = np.array([64, 128, 256, 2048, 2048])*self.mul


__all__ = [
    'VGG', 'vgg11', 'vgg11_bn', 'vgg13', 'vgg13_bn', 'vgg16', 'vgg16_bn',
    'vgg19_bn', 'vgg19',
]


class VGG(_DynamicModel):
    '''
    VGG model 
    '''
    def __init__(self, input_dim, output_dim, cfg, batch_norm=False):
        super(VGG, self).__init__()

        n_channels, size, _ = input_dim
        self.layers = make_layers(cfg, n_channels, batch_norm=batch_norm)

        self.smid = size
        for m in self.layers:
            if isinstance(m, DynamicConv2D) or isinstance(m, nn.MaxPool2d):
                try:
                    self.smid = compute_conv_output_size(self.smid, m.kernel_size[0], m.stride[0], m.padding[0], m.dilation[0])
                except:
                    self.smid = compute_conv_output_size(self.smid, m.kernel_size, m.stride, m.padding, m.dilation)

        self.layers += nn.ModuleList([
            nn.Flatten(),
            nn.Dropout(),
            DynamicLinear(512*self.smid*self.smid, 512),
            nn.ReLU(True),
            nn.Dropout(),
            DynamicLinear(512, 512),
            nn.ReLU(True),
            DynamicLinear(512, output_dim),
        ])
         # Initialize weights
        # for m in self.modules():
        #     if isinstance(m, nn.Conv2d):
        #         n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
        #         m.weight.data.normal_(0, math.sqrt(2. / n))
        #         m.bias.data.zero_()

        self.DM = [m for m in self.modules() if isinstance(m, _DynamicLayer)]



def make_layers(cfg, n_channels, batch_norm=False):
    layers = []
    in_channels = n_channels
    for v in cfg:
        if v == 'M':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2)]
        else:
            conv2d = DynamicConv2D(in_channels, v, kernel_size=3, padding=1, batch_norm=batch_norm, bias=False)
            layers += [conv2d, nn.ReLU(inplace=True)]
            in_channels = v
    return nn.ModuleList(layers)


cfg = {
    'A': [64, 'M', 128, 'M', 256, 256, 'M', 512, 512, 'M', 512, 512, 'M'],
    'B': [64, 64, 'M', 128, 128, 'M', 256, 256, 'M', 512, 512, 'M', 512, 512, 'M'],
    'D': [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 'M', 512, 512, 512, 'M', 512, 512, 512, 'M'],
    'E': [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 256, 'M', 512, 512, 512, 512, 'M', 
          512, 512, 512, 512, 'M'],
}


def vgg11(input_dim, output_dim):
    """VGG 11-layer model (configuration "A")"""
    return VGG(input_dim, output_dim, cfg['A'], batch_norm=False)


def vgg11_bn(input_dim, output_dim):
    """VGG 11-layer model (configuration "A") with batch normalization"""
    return VGG(input_dim, output_dim, cfg['A'], batch_norm=True)


def vgg13(input_dim, output_dim):
    """VGG 13-layer model (configuration "B")"""
    return VGG(input_dim, output_dim, cfg['B'], batch_norm=False)


def vgg13_bn(input_dim, output_dim):
    """VGG 13-layer model (configuration "B") with batch normalization"""
    return VGG(input_dim, output_dim, cfg['B'], batch_norm=True)


def vgg16(input_dim, output_dim):
    """VGG 16-layer model (configuration "D")"""
    return VGG(input_dim, output_dim, cfg['C'], batch_norm=False)


def vgg16_bn(input_dim, output_dim):
    """VGG 16-layer model (configuration "D") with batch normalization"""
    return VGG(input_dim, output_dim, cfg['C'], batch_norm=True)


def vgg19(input_dim, output_dim):
    """VGG 19-layer model (configuration "E")"""
    return VGG(input_dim, output_dim, cfg['E'], batch_norm=False)


def vgg19_bn(input_dim, output_dim):
    """VGG 19-layer model (configuration 'E') with batch normalization"""
    return VGG(input_dim, output_dim, cfg['E'], batch_norm=True)


class LeNet5(_DynamicModel):

    def __init__(self, input_dim, output_dim):
        super(LeNet5, self).__init__()
        
        n_channels, size, _ = input_dim
        self.layers = nn.ModuleList([         
            DynamicConv2D(n_channels, 6, kernel_size=5, stride=1),
            nn.ReLU(),
            nn.MaxPool2d(kernel_size=2),
            DynamicConv2D(6, 16, kernel_size=5, stride=1),
            nn.ReLU(),
            nn.MaxPool2d(kernel_size=2),
        ])

        self.smid = size
        for m in self.layers:
            if isinstance(m, DynamicConv2D) or isinstance(m, nn.MaxPool2d):
                try:
                    self.smid = compute_conv_output_size(self.smid, m.kernel_size[0], m.stride[0], m.padding[0], m.dilation[0])
                except:
                    self.smid = compute_conv_output_size(self.smid, m.kernel_size, m.stride, m.padding, m.dilation)


        self.layers += nn.ModuleList([
            nn.Flatten(),
            DynamicLinear(16*self.smid*self.smid, 120),
            nn.ReLU(),
            DynamicLinear(120, 84),
            nn.ReLU(),
            DynamicLinear(84, output_dim),
        ])

        self.DM = [m for m in self.layers if isinstance(m, _DynamicLayer)]
