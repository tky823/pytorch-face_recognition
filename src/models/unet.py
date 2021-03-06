import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.modules.utils import _pair

from conv import DepthwiseSeparableConv2d, DepthwiseSeparableConvTranspose2d

class UNetBase(nn.Module):
    def __init__(self):
        super().__init__()
        
    @classmethod
    def load_model(cls, model_path):
        package = torch.load(model_path)
        
        channels = package['channels']
        kernel_size, stride, dilated = package['kernel_size'], package['stride'], package['dilated']
        separable = package['separable']
        nonlinear_enc, nonlinear_dec = package['nonlinear_enc'], package['nonlinear_dec']
        out_channels = package['out_channels']
        
        model = cls(channels, kernel_size, stride=stride, dilated=dilated, separable=separable, nonlinear_enc=nonlinear_enc, nonlinear_dec=nonlinear_dec, out_channels=out_channels)
        model.load_state_dict(package['state_dict'])
        
        return model
        
    def get_package(self):
        package = {
            'channels': self.channels,
            'kernel_size': self.kernel_size,
            'stride': self.stride,
            'dilated': self.dilated,
            'separable': self.separable,
            'nonlinear_enc': self.nonlinear_enc,
            'nonlinear_dec': self.nonlinear_dec,
            'out_channels': self.out_channels
        }
        
        return package
        
    def _get_num_parameters(self):
        num_parameters = 0
        
        for p in self.parameters():
            if p.requires_grad:
                num_parameters += p.numel()
                
        return num_parameters


class UNet2d(UNetBase):
    def __init__(self, channels, kernel_size, stride=None, dilated=False, separable=False, nonlinear_enc='relu', nonlinear_dec='relu', out_channels=None):
        """
        Args:
            channels <list<int>>
            out_channels <int>
        """
        super().__init__()
        
        channels_enc = channels
        
        if out_channels is None:
            channels_dec = channels[::-1]
        else:
            channels_dec = channels[:0:-1] + [out_channels]
            
        _channels_dec = []
        
        for idx, out_channel in enumerate(channels_dec):
            if idx == 0:
                _channels_dec.append(out_channel)
            else:
                _channels_dec.append(2 * out_channel)
                
        channels_dec = _channels_dec
            
        
        self.channels = channels
        self.kernel_size, self.stride, self.dilated = kernel_size, stride, dilated
        self.separable = separable
        self.nonlinear_enc, self.nonlinear_dec = nonlinear_enc, nonlinear_dec
        self.out_channels = out_channels

        self.encoder = Encoder2d(channels_enc, kernel_size=kernel_size, stride=stride, dilated=dilated, separable=separable, nonlinear=nonlinear_enc)
        self.bottleneck = nn.Conv2d(channels[-1], channels[-1], kernel_size=(1,1), stride=(1,1))
        self.decoder = Decoder2d(channels_dec, kernel_size=kernel_size, stride=stride, dilated=dilated, separable=separable, nonlinear=nonlinear_dec)
        
    def forward(self, input):
        x, skip = self.encoder(input)
        x = self.bottleneck(x)
        output = self.decoder(x, skip[::-1])
        
        return output

"""
    Encoder
"""

class Encoder2d(nn.Module):
    def __init__(self, channels, kernel_size, stride=None, dilated=False, separable=False, nonlinear='relu'):
        """
        Args:
            channels <list<int>>
            kernel_size <tuple<int,int>> or <list<tuple<int,int>>>
            stride <tuple<int,int>> or <list<tuple<int,int>>>
            dilated <bool>
            nonlinear <str> or <list<str>>
        """
        super().__init__()
        
        n_blocks = len(channels) - 1
        
        if type(kernel_size) is not list:
            kernel_size = [kernel_size] * n_blocks
        if stride is None:
            stride = kernel_size
        elif type(stride) is not list:
            stride = [stride] * n_blocks
        
        if type(nonlinear) is not list:
            nonlinear = [nonlinear] * n_blocks
        
        self.n_blocks = n_blocks
        
        net = []
        
        for n in range(n_blocks):
            if dilated:
                dilation = 2**n
                assert stride[n] == 1, "stride must be 1 when dilated convolution."
            else:
                dilation = 1
            net.append(EncoderBlock2d(channels[n], channels[n+1], kernel_size=kernel_size[n], stride=stride[n], dilation=dilation, separable=separable, nonlinear=nonlinear[n]))
        
        self.net = nn.Sequential(*net)
        
    def forward(self, input):
        n_blocks = self.n_blocks
        
        x = input
        skip = []
        
        for n in range(n_blocks):
            x = self.net[n](x)
            skip.append(x)
        
        return x, skip
        
"""
    Decoder
"""

class Decoder2d(nn.Module):
    def __init__(self, channels, kernel_size, stride=None, dilated=False, separable=False, nonlinear='relu'):
        """
        Args:
            channels <list<int>>
            kernel_size <tuple<int,int>> or <list<tuple<int,int>>>
            stride <tuple<int,int>> or <list<tuple<int,int>>>
            dilated <bool>
            nonlinear <str> or <list<str>>
        """
        super().__init__()
        
        n_blocks = len(channels) - 1
        
        if type(kernel_size) is not list:
            kernel_size = [kernel_size] * n_blocks
        if stride is None:
            stride = kernel_size
        elif type(stride) is not list:
            stride = [stride] * n_blocks
        if type(nonlinear) is not list:
            nonlinear = [nonlinear] * n_blocks
            
        self.n_blocks = n_blocks
        
        net = []
        
        for n in range(n_blocks):
            if dilated:
                dilation = 2**(n_blocks - n - 1)
                assert stride[n] == 1, "stride must be 1 when dilated convolution."
            else:
                dilation = 1
            net.append(DecoderBlock2d(channels[n], channels[n+1]//2, kernel_size=kernel_size[n], stride=stride[n], dilation=dilation, separable=separable, nonlinear=nonlinear[n]))
            # channels[n+1]//2: because of skip connection
        
        self.net = nn.Sequential(*net)
            
    def forward(self, input, skip):
        """
        Args:
            input (batch_size, C1, H, W)
            skip <list<torch.Tensor>>
        """
        n_blocks = self.n_blocks
        
        x = input
        
        for n in range(n_blocks):
            if n == 0:
                x = self.net[n](x)
            else:
                x = self.net[n](x, skip[n])
        output = x
        
        return output


"""
    Encoder Block
"""

class EncoderBlock2d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride=None, dilation=1, separable=False, nonlinear='relu'):
        super().__init__()
        
        kernel_size = _pair(kernel_size)
        
        if stride is None:
            stride = kernel_size
        stride = _pair(stride)
        dilation = _pair(dilation)
    
        self.kernel_size, self.stride, self.dilation = kernel_size, stride, dilation
    
        if separable:
            self.conv2d = DepthwiseSeparableConv2d(in_channels, out_channels, kernel_size=kernel_size, stride=stride, dilation=dilation)
        else:
            self.conv2d = nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, stride=stride, dilation=dilation)
        self.batch_norm2d = nn.BatchNorm2d(out_channels)
        
        if nonlinear == 'relu':
            self.nonlinear = nn.ReLU()
        else:
            raise NotImplementedError()
            
    def forward(self, input):
        """
        Args:
            input (batch_size, C, H, W)
        """
        Kh, Kw = self.kernel_size
        Sh, Sw = self.stride
        Dh, Dw = self.dilation
        
        Kh = (Kh - 1) * Dh + 1
        Kw = (Kw - 1) * Dw + 1
        
        _, _, H, W = input.size()
        padding_height = Kh - 1 - (Sh - (H - Kh) % Sh) % Sh
        padding_width = Kw - 1 - (Sw - (W - Kw) % Sw) % Sw
        padding_top = padding_height // 2
        padding_bottom = padding_height - padding_top
        padding_left = padding_width // 2
        padding_right = padding_width - padding_left
        
        input = F.pad(input, (padding_left, padding_right, padding_top, padding_bottom))
        
        x = self.conv2d(input)
        x = self.batch_norm2d(x)
        output = self.nonlinear(x)
        
        return output

        
"""
    Decoder Block
"""

class DecoderBlock2d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride=None, dilation=1, separable=False, nonlinear='relu'):
        super().__init__()
        
        kernel_size = _pair(kernel_size)
        
        if stride is None:
            stride = kernel_size
        stride = _pair(stride)
        dilation = _pair(dilation)

        self.kernel_size, self.stride, self.dilation = kernel_size, stride, dilation

        if separable:
            self.deconv2d = DepthwiseSeparableConvTranspose2d(in_channels, out_channels, kernel_size=kernel_size, stride=stride, dilation=dilation)
        else:
            self.deconv2d = nn.ConvTranspose2d(in_channels, out_channels, kernel_size=kernel_size, stride=stride, dilation=dilation)
        self.batch_norm2d = nn.BatchNorm2d(out_channels)
        
        if nonlinear == 'relu':
            self.nonlinear = nn.ReLU()
        elif nonlinear == 'sigmoid':
            self.nonlinear = nn.Sigmoid()
        else:
            raise NotImplementedError()
            
    def forward(self, input, skip=None):
        """
        Args:
            input (batch_size, C1, H, W)
            skip (batch_size, C2, H, W)
                where C = C1 + C2
        """
        Kh, Kw = self.kernel_size
        Sh, Sw = self.stride
        Dh, Dw = self.dilation
        
        Kh = (Kh - 1) * Dh + 1
        Kw = (Kw - 1) * Dw + 1
        
        padding_height = Kh - Sh
        padding_width = Kw - Sw
        padding_top = padding_height//2
        padding_bottom = padding_height - padding_top
        padding_left = padding_width//2
        padding_right = padding_width - padding_left
    
        if skip is not None:
            input = torch.cat([input, skip], dim=1)

        x = self.deconv2d(input)
        x = F.pad(x, (-padding_left, -padding_right, -padding_top, -padding_bottom))
        x = self.batch_norm2d(x)
        output = self.nonlinear(x)
        
        return output


if __name__ == '__main__':
    batch_size = 4
    C = 3
    channels = [C, 8, 16, 16, 32]
    out_channels = 32
    H, W = 256, 256
    
    kernel_size, stride, dilated = 3, 1, True
    separable = False
    nonlinear_enc = 'relu'
    nonlinear_dec = ['relu', 'relu', 'relu', 'sigmoid']
    
    input = torch.randint(0, 5, (batch_size, C, H, W)).float()
    print(input.size())
    
    unet2d = UNet2d(channels, kernel_size=kernel_size, stride=stride, dilated=dilated, separable=separable, nonlinear_enc=nonlinear_enc, nonlinear_dec=nonlinear_dec, out_channels=out_channels)
    print(unet2d)
    
    print("# Parameters:", unet2d._get_num_parameters())
    
    output = unet2d(input)
    print(output.size())
    
    package = unet2d.get_package()
    package['state_dict'] = unet2d.state_dict()
    model_path = "u_net.pth"
    torch.save(package, model_path)
    model = UNet2d.load_model(model_path)
