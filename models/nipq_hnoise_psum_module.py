import numpy as np
import torch
import math
import os
import torch.nn.functional as F
import pandas as pd
import utils.padding as Pad
from .bitserial_modules import *
from .split_modules import *
from .quantized_basic_modules import psum_quant_merge
from .nipq_quantization_module import QuantActs, Quantizer
# custom kernel
import conv_sweight_cuda

"""
    This module does not support training mode in nipq quantization
    So, nipq quantization noise + hardware noise are not used.
"""

# split convolution layer across input channel
def split_conv(weight, nWL):
    nIC = weight.shape[1]
    nWH = weight.shape[2]*weight.shape[3]
    nMem = int(math.ceil(nIC/math.floor(nWL/nWH)))
    nIC_list = [int(math.floor(nIC/nMem)) for _ in range(nMem)]
    for idx in range((nIC-nIC_list[0]*nMem)):
        nIC_list[idx] += 1

    return nIC_list

# split fully connected layer across input channel
def split_linear(weight, nWL):
    nIF = weight.shape[1] #in_features
    nMem = int(math.ceil(nIF/nWL))
    nIF_list = [int(math.floor(nIF/nMem)) for _ in range(nMem)]
    for idx in range((nIF-nIF_list[0]*nMem)):
        nIF_list[idx] += 1

    return nIF_list

def calculate_groups(arraySize, fan_in):
    if arraySize > 0:
        # groups
        groups = int(np.ceil(fan_in / arraySize))
        while fan_in % groups != 0:
            groups += 1
    else:
        groups = 1

    return groups

class Psum_QConv2d(SplitConv):
    """
        Quant(Nipq) Conv + Psum quantization
    """
    def __init__(self, *args, act_func=None, padding=0, padding_mode='zeros', **kargs):
        super(Psum_QConv2d, self).__init__(*args,  **kargs)
        self.act_func = act_func
        self.padding = padding
        self.padding_mode = padding_mode
        
        if self.padding_mode == 'zeros':
            self.padding_value = 0
        elif self.padding_mode == 'ones':
            self.padding_value = 1
        elif self.padding_mode == 'alter':
            self.padding_value = 0

        self.quant_func = Quantizer(sym=True, noise=False, offset=0, is_stochastic=True, is_discretize=True)
        self.bits = self.quant_func.get_bit()

        ## for psum quantization
        self.mapping_mode = '2T2R' # Array mapping method [2T2R, ref_a]]
        self.wbit_serial = None
        self.cbits = 4
        self.psum_mode = None
        self.pclipmode = 'layer'
        self.pbits = 32
        # for scan version
        self.pstep = None
        self.pzero = None  # contain zero value (True)
        self.center = None
        self.pbound = None
        # for sigma version
        self.pclip = None
        self.psigma = None

        # for logging
        self.bitserial_log = False
        self.layer_idx = -1
        self.checkpoint = None
        self.info_print = True

    def model_split_groups(self, arraySize):
        self.split_groups = calculate_groups(arraySize, self.fan_in)
        if self.fan_in % self.split_groups != 0:
            raise ValueError('fan_in must be divisible by groups')
        self.group_fan_in = int(self.fan_in / self.split_groups)
        self.group_in_channels = int(np.ceil(self.in_channels / self.split_groups))
        residual = self.group_fan_in % self.kSpatial
        if residual != 0:
            if self.kSpatial % residual != 0:
                self.group_in_channels += 1
        ## log move group for masking & group convolution
        self.group_move_in_channels = torch.zeros(self.split_groups-1, dtype=torch.int)
        group_in_offset = torch.zeros(self.split_groups, dtype=torch.int).to(self.weight.device)
        self.register_buffer('group_in_offset', group_in_offset)
        ## get group conv info
        self._group_move_offset()

        # sweight
        sweight = torch.Tensor(self.out_channels*self.split_groups, self.group_in_channels, self.kernel_size[0], self.kernel_size[1]).to(self.weight.device)
        self.register_buffer('sweight', sweight)

    def setting_pquant_func(self, pbits=None, center=[], pbound=None):
        # setting options for pquant func
        if pbits is not None:
            self.pbits = pbits
        if pbound is not None:
            self.pbound = pbound
            # get pquant step size
            self.pstep = 2 * self.pbound / ((2.**self.pbits) - 1)

        if (self.mapping_mode == 'two_com') or (self.mapping_mode == 'ref_d') or (self.mapping_mode == 'PN'):
            self.pzero = False
        else:
            self.pzero = True

        # get half of pquant levels
        self.phalf_num_levels = 2.**(self.pbits-1)

        # get center value
        self.setting_center(center=center)

    def setting_center(self, center=[]):
        if self.pzero:
            self.center = 0 # center is zero value in 2T2R mode
        else:
        # centor's role is offset in two_com mode or ref_d mode
            self.center = center

    def reset_layer(self, wbit_serial=None, groups=None, 
                        pbits=None, pbound=None, center=[]):
        if wbit_serial is not None:
            self.wbit_serial = wbit_serial

        self.setting_pquant_func(pbits=pbits, center=center, pbound=pbound)
    
    def bitserial_act_split(self, input):
        """
            input: [batch, channel, H, W]
            output: [batch, abits * channel, H, W]
        """
        bits = self.bits
        a_scale = self.act_func.quant_func.get_alpha()
        int_input = input / a_scale  # remove remainder value ex).9999 
        
        output_dtype = int_input.round_().dtype
        output_uint8= int_input.to(torch.uint8)
        # bitserial_step = 1 / (2.**(bits - 1.))

        output = output_uint8 & 1
        for i in range(1, bits):
            out_tmp = output_uint8 & (1 << i)
            output = torch.cat((output, out_tmp), 1)
        output = output.to(output_dtype)
        # output.mul_(bitserial_step) ## for preventing overflow

        return output.round_(), a_scale
    
    def _bitserial_log_forward(self, input):
        print(f'[layer{self.layer_idx}]: bitserial mac log')
        # delete padding_shpe & additional padding operation by matching padding/stride format with nn.Conv2d
        if self.padding > 0:
            padding_shape = (self.padding, self.padding, self.padding, self.padding)
            input = Pad.pad(input, padding_shape, self.padding_mode, self.padding_value)

        # local parameter settings
        bitplane_idx = 0

        with torch.no_grad():
            # get quantization parameter and input bitserial 
            bits = self.bits
            qweight = self.quant_func(self.weight, self.training)
            w_scale = self.quant_func.get_alpha()
            sinput, a_scale = self.bitserial_act_split(input)
            
            ## get dataframe
            logger = f'{self.checkpoint}/layer{self.layer_idx}_mac_static.pkl'
            df = pd.DataFrame(columns=['wbits', 'abits', 'mean', 'std', 'min', 'max'])

            layer_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_hist.pkl'
            network_hist = f'{self.checkpoint}/hist/network_hist.pkl'

            #plane hist
            
            ### in-mem computation mimic (split conv & psum quant/merge)
            input_chunk = torch.chunk(sinput, bits, dim=1)
            self.sweight = conv_sweight_cuda.forward(self.sweight, qweight/w_scale, self.group_in_offset, self.split_groups)
            weight_chunk = torch.chunk(self.sweight, 1, dim=1)

            psum_scale = w_scale * a_scale 

            out_tmp = None
            layer_hist_dict = {}
            for abit, input_s in enumerate(input_chunk):
                abitplane_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_a:{abit}_hist.pkl'
                a_hist_dict = {}
                for wbit, weight_s in enumerate(weight_chunk):
                    wabitplane_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_w:{wbit}_a:{abit}_hist.pkl'
                    wa_hist_dict = {}
                    a_mag = 2**(abit)
                    import pdb; pdb.set_trace()
                    out_tmp = self._split_forward((input_s/a_mag).round(), weight_s, padded=True, ignore_bias=True,
                                                    weight_is_split=True, infer_only=True) 

                    ## NOTE
                    df.loc[bitplane_idx] = [wbit, abit,
                                                    float(out_tmp.mean()), 
                                                    float(out_tmp.std()), 
                                                    float(out_tmp.min()), 
                                                    float(out_tmp.max())] 

                    out_min = out_tmp.min()
                    out_max = out_tmp.max()

                    # update hist
                    for val in range(int(out_min), int(out_max)+1):
                        count = out_tmp.eq(val).sum().item()
                        # get wa_hist
                        wa_hist_dict[val] = count
                        # get w_hist
                        if val in a_hist_dict.keys():
                            a_hist_dict[val] += count
                        else:
                            a_hist_dict[val] = count

                    # save wabitplane_hist
                    df_hist = pd.DataFrame(list(wa_hist_dict.items()), columns = ['val', 'count'])
                    # wabitplane hist
                    if os.path.isfile(wabitplane_hist):
                        print(f'[{self.layer_idx}] Update wabitplane_hist for w:{wbit}/a:{abit} ({wabitplane_hist})')
                        df_wabitplane_hist = pd.read_pickle(wabitplane_hist) 
                        df_merge = pd.merge(df_wabitplane_hist, df_hist, how="outer", on="val")
                        df_merge = df_merge.replace(np.nan, 0)
                        df_merge['count'] = df_merge['count_x'] + df_merge['count_y']
                        df_merge = df_merge[['val', 'count']]
                        df_merge.to_pickle(wabitplane_hist)
                    else:
                        print(f'[{self.layer_idx}] Create wabitplane_hist for w:{wbit}/a:{abit} ({wabitplane_hist})')
                        df_hist.to_pickle(wabitplane_hist)

                    # split output merge
                    output_chunk = (out_tmp*a_mag).chunk(self.split_groups, dim=1)
                    import pdb; pdb.set_trace()
                    for g in range(0, self.split_groups):
                        if g==0:
                            out_tmp = output_chunk[g]
                        else:
                            out_tmp += output_chunk[g]

                    bitplane_idx += 1

                # save abitplane_hist
                df_hist = pd.DataFrame(list(a_hist_dict.items()), columns = ['val', 'count'])
                # wbitplane hist
                if os.path.isfile(abitplane_hist):
                    print(f'[{self.layer_idx}]Update abitplane_hist for a:{abit} ({abitplane_hist})')
                    df_abitplane_hist = pd.read_pickle(abitplane_hist) 
                    df_merge = pd.merge(df_abitplane_hist, df_hist, how="outer", on="val")
                    df_merge = df_merge.replace(np.nan, 0)
                    df_merge['count'] = df_merge['count_x'] + df_merge['count_y']
                    df_merge = df_merge[['val', 'count']]
                    df_merge.to_pickle(abitplane_hist)
                else:
                    print(f'[{self.layer_idx}]Create abitplane_hist for a:{abit} ({abitplane_hist})')
                    df_hist.to_pickle(abitplane_hist)

                # update layer hist
                for val, count in a_hist_dict.items():
                    if val in layer_hist_dict.keys():
                        layer_hist_dict[val] += count
                    else:
                        layer_hist_dict[val] = count
                
                output = out_tmp if abit == 0 else output+out_tmp

            # restore output's scale
            output = output * psum_scale

            # add bias
            if self.bias is not None:
                output += self.bias

        # save logger
        df.to_pickle(logger)
        # df_scaled.to_pickle(logger_scaled)

        # save hist
        df_hist = pd.DataFrame(list(layer_hist_dict.items()), columns = ['val', 'count'])
        # layer hist
        if os.path.isfile(layer_hist):
            print(f'[{self.layer_idx}] Update layer_hist ({layer_hist})')
            df_layer_hist = pd.read_pickle(layer_hist) 
            df_merge = pd.merge(df_layer_hist, df_hist, how="outer", on="val")
            df_merge = df_merge.replace(np.nan, 0)
            df_merge['count'] = df_merge['count_x'] + df_merge['count_y']
            df_merge = df_merge[['val', 'count']]
            df_merge.to_pickle(layer_hist)
        else:
            print(f'[{self.layer_idx}] Create layer_hist ({layer_hist})')
            df_hist.to_pickle(layer_hist)
        # network hist
        if os.path.isfile(network_hist):
            print(f'[{self.layer_idx}]Update network_hist ({network_hist})')
            df_network_hist = pd.read_pickle(network_hist) 
            df_merge = pd.merge(df_network_hist, df_hist, how="outer", on="val")
            df_merge = df_merge.replace(np.nan, 0)
            df_merge['count'] = df_merge['count_x'] + df_merge['count_y']
            df_merge = df_merge[['val', 'count']]
            df_merge.to_pickle(network_hist)
        else:
            print(f'[{self.layer_idx}] Create network_hist ({network_hist})')
            df_hist.to_pickle(network_hist)

        # output_real = F.conv2d(input, qweight, bias=self.bias,
        #                     stride=self.stride, dilation=self.dilation, groups=self.groups)
        # import pdb; pdb.set_trace()

        return output

    def _ADC_clamp_value(self):
        # get ADC clipping value for hist [Layer or Network hist]
        if self.pclipmode == 'layer':
            phist = f'{self.checkpoint}/hist/layer{self.layer_idx}_hist.pkl'
            # phist = f'./hist/layer{self.layer_idx}_hist.pkl'
        elif self.pclipmode == 'network':
            phist = f'{self.checkpoint}/hist/network_hist.pkl'

        if os.path.isfile(phist):
            # print(f'Load ADC_hist ({phist})')
            df_hist = pd.read_pickle(phist)
            mean, std, min, max = get_statistics_from_hist(df_hist)
        else:
            if self.pbits != 32:
                assert False, "Error: Don't have ADC hist file"
            else:
                mean, std, min, max = 0.0, 0.0, 0.0, 0.0

        # Why abs(mean) is used not mean?? => Asymmetric quantizaion is occured
        if self.pbits == 32:
            maxVal = 1
            minVal = 0
        else:
            if self.pclip == 'max':
                maxVal = max
                minVal = min
            else:
                maxVal =  (abs(mean) + self.psigma*std).round() 
                minVal = (abs(mean) - self.psigma*std).round()
                if (self.mapping_mode == 'two_com') or (self.mapping_mode =='ref_d') or (self.mapping_mode == 'PN'):
                    minVal = min if minVal < 0 else minVal
        
        midVal = (maxVal + minVal) / 2

        if self.info_print:
            write_file = f'{self.checkpoint}/Layer_clipping_range.txt'
            if os.path.isfile(write_file) and (self.layer_idx == 0):
                option = 'w'
            else:
                option = 'a'
            with open(write_file, option) as file:
                if self.layer_idx == 0:
                    file.write(f'{self.pclipmode}-wise Mode Psum quantization \n')
                    file.write(f'Layer_information  Mean    Std     Min Max Clip_Min    Clip_Max    Mid \n')
                file.write(f'Layer{self.layer_idx}  {mean}  {std}   {min}   {max}   {minVal}    {maxVal}    {midVal}\n')
            
            print(f'{self.pclipmode}-wise Mode Psum quantization')
            if self.pbits == 32:
                print(f'Layer{self.layer_idx} information | pbits {self.pbits}')
            else:
                print(f'Layer{self.layer_idx} information | pbits {self.pbits} | Mean: {mean} | Std: {std} | Min: {min} | Max: {max} | Clip Min: {minVal} | Clip Max: {maxVal} | Mid: {midVal}')
            self.info_print = False

        return minVal, maxVal, midVal 

    def _bitserial_comp_forward(self, input):
        # delete padding_shpe & additional padding operation by matching padding/stride format with nn.Conv2d
        if self.padding > 0:
            padding_shape = (self.padding, self.padding, self.padding, self.padding)
            input = Pad.pad(input, padding_shape, self.padding_mode, self.padding_value)

        # get quantization parameter and input bitserial 
        bits = self.bits
        qweight = self.quant_func(self.weight, self.training)

        if self.wbit_serial:
            with torch.no_grad():
                w_scale = self.quant_func.get_alpha()
                sinput, a_scale = self.bitserial_act_split(input)

                if self.psum_mode == 'sigma':
                    minVal, maxVal, midVal = self._ADC_clamp_value()
                    self.setting_pquant_func(pbits=self.pbits, center=minVal, pbound=midVal-minVal)
                elif self.psum_mode == 'scan':
                    pass
                else:
                    assert False, 'This script does not support {self.psum_mode}'

                ### in-mem computation mimic (split conv & psum quant/merge)
                input_chunk = torch.chunk(sinput, bits, dim=1)
                self.sweight = conv_sweight_cuda.forward(self.sweight, qweight/w_scale, self.group_in_offset, self.split_groups)
                weight_chunk = torch.chunk(self.sweight, 1, dim=1)

                psum_scale = w_scale * a_scale

                for abit, input_s in enumerate(input_chunk):
                    out_adc = None
                    for wbit, weight_s in enumerate(weight_chunk):
                        out_tmp = self._split_forward(input_s, weight_s, padded=True, ignore_bias=True, cat_output=False,
                                                weight_is_split=True, infer_only=True)

                        a_mag = 2**(abit)
                        out_adc = psum_quant_merge(out_adc, out_tmp,
                                                    pbits=self.pbits, step=self.pstep, 
                                                    half_num_levels=self.phalf_num_levels, 
                                                    pbound=self.pbound, center=self.center, weight=a_mag,
                                                    groups=self.split_groups, pzero=self.pzero)

                    output = out_adc if abit == 0 else output+out_adc

                # restore output's scale
                output = output * psum_scale
        else:
            # no serial computation with psum computation
            import pdb; pdb.set_trace()
            self.pbits = 32
            output = self._split_forward(input, qweight, padded=True, ignore_bias=True, merge_group=True)

        # add bias
        if self.bias is not None:
            output += self.bias

        # output_real = F.conv2d(input, qweight, bias=self.bias,
        #                         stride=self.stride, dilation=self.dilation, groups=self.groups)
        # import pdb; pdb.set_trace()

        return output

    def forward(self, x):
        if self.act_func is not None:
            x = self.act_func(x)
    
        if self.bitserial_log:
            return self._bitserial_log_forward(x)
        else:
            return self._bitserial_comp_forward(x)

    def extra_repr(self):
        """Provides layer information, including wbits, when print(model) is called."""
        s = ('{in_channels}, {out_channels}, kernel_size={kernel_size}'
             ', stride={stride}')
        if self.padding != 0:
            s += ', padding={padding}'
        if self.dilation != (1,) * len(self.dilation):
            s += ', dilation={dilation}'
        if self.groups != 1:
            s += ', groups={groups}'
        if self.bias is None:
            s += ', bias=False'
        # s += ', bits={bits}, wbit_serial={wbit_serial}'
        # s += ', split_groups={split_groups}, mapping_mode={mapping_mode}, cbits={cbits}'
        # s += ', psum_mode={psum_mode}, pbits={pbits}, pbound={pbound}'
        # s += ', bitserial_log={bitserial_log}, layer_idx={layer_idx}'            
        return s.format(**self.__dict__)

class Psum_QLinear(SplitLinear):
    """
        Quant(LSQ)Linear + Psum quantization
    """
    def __init__(self, *args, act_func=None, **kargs):
        super(Psum_QLinear, self).__init__(*args, **kargs)
        self.act_func = act_func

        self.quant_func = Quantizer(sym=True, noise=False, offset=0, is_stochastic=True, is_discretize=True)
        self.bits = self.quant_func.get_bit()

        # for psum quantization
        self.mapping_mode = '2T2R' # Array mapping method [2T2R, ref_a]
        self.wbit_serial = None
        self.cbits = 4
        self.psum_mode = None
        self.pclipmode = 'layer'
        self.pbits = 32
        # for scan version
        self.pstep = None
        self.pzero = None # contain zero value (True)
        self.center = None
        self.pbound = None
        # for sigma version
        self.pclip = None
        self.psigma = None

        # for logging
        self.bitserial_log = False
        self.layer_idx = -1
        self.checkpoint = None
        self.info_print = True

    def model_split_groups(self, arraySize=0):
        self.split_groups = calculate_groups(arraySize, self.in_features)
        if self.in_features % self.split_groups != 0:
            raise ValueError('in_features must be divisible by groups')
        self.group_in_features = int(self.in_features / self.split_groups)

    def setting_pquant_func(self, pbits=None, center=[], pbound=None):
        # setting options for pquant func
        if pbits is not None:
            self.pbits = pbits
        if pbound is not None:
            self.pbound = pbound
            # get pquant step size
            self.pstep = 2 * self.pbound / ((2.**self.pbits) - 1)

        if (self.mapping_mode == 'two_com') or (self.mapping_mode == 'ref_d') or (self.mapping_mode == 'PN'):
            self.pzero = False
        else:
            self.pzero = True

        # get half of pquant levels
        self.phalf_num_levels = 2.**(self.pbits-1)

        # get center value
        self.setting_center(center=center)
    
    def setting_center(self, center=[]):
        if self.pzero:
            self.center = 0 # center is zero value in 2T2R mode
        else:
        # centor's role is offset in two_com mode or ref_d mode
            self.center = center
    
    def reset_layer(self, wbit_serial=None, groups=None, 
                        pbits=None, pbound=None, center=[]):
        if wbit_serial is not None:
            self.wbit_serial = wbit_serial

        self.setting_pquant_func(pbits=pbits, center=center, pbound=pbound)
    
    def bitserial_act_split(self, input):
        """
            input: [batch, channel, H, W]
            output: [batch, abits * channel, H, W]
        """
        bits = self.bits
        a_scale = self.act_func.quant_func.get_alpha()
        int_input = input / a_scale  # remove remainder value ex).9999 
        
        output_dtype = int_input.round_().dtype
        output_uint8= int_input.to(torch.uint8)
        # bitserial_step = 1 / (2.**(bits - 1.))

        output = output_uint8 & 1
        for i in range(1, bits):
            out_tmp = output_uint8 & (1 << i)
            output = torch.cat((output, out_tmp), 1)
        output = output.to(output_dtype)
        # output.mul_(bitserial_step) ## for preventing overflow

        return output.round_(), a_scale

    def _bitserial_log_forward(self, input):
        print(f'[layer{self.layer_idx}]: bitserial mac log')

        # local parameter setting
        bitplane_idx = 0

        # get quantization parameter and input bitserial
        bits = self.bits 
        qweight = self.quant_func(self.weight, self.training)
        w_scale = self.quant_func.get_alpha()
        sinput, a_scale = self.bitserial_act_split(input)


        ## get dataframe
        logger = f'{self.checkpoint}/layer{self.layer_idx}_mac_static.pkl'
        df = pd.DataFrame(columns=['wbits', 'abits', 'mean', 'std', 'min', 'max'])

        layer_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_hist.pkl'
        network_hist = f'{self.checkpoint}/hist/network_hist.pkl'
        
        ### in-mem computation mimic (split conv & psum quant/merge)
        input_chunk = torch.chunk(sinput, bits, dim=1)
        weight_chunk = torch.chunk(qweight/w_scale, 1, dim=1)

        psum_scale = w_scale * a_scale

        out_tmp = None
        layer_hist_dict = {}
        for abit, input_s in enumerate(input_chunk):
            abitplane_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_a:{abit}_hist.pkl'
            a_hist_dict = {}
            for wbit, weight_s in enumerate(weight_chunk):
                wabitplane_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_w:{wbit}_a:{abit}_hist.pkl'
                wa_hist_dict = {}
                a_mag = 2**(abit)
                import pdb; pdb.set_trace()
                out_tmp = self._split_forward((input_s/a_mag).round(), weight_s, ignore_bias=True, infer_only=True)
                                
                ## NOTE
                df.loc[bitplane_idx] = [wbit, abit,
                                                float(out_tmp.mean()), 
                                                float(out_tmp.std()), 
                                                float(out_tmp.min()), 
                                                float(out_tmp.max())] 

                out_min = out_tmp.min()
                out_max = out_tmp.max()

                # update hist
                for val in range(int(out_min), int(out_max)+1):
                    count = out_tmp.eq(val).sum().item()
                    # get wa_hist
                    wa_hist_dict[val] = count
                    # get w_hist
                    if val in a_hist_dict.keys():
                        a_hist_dict[val] += count
                    else:
                        a_hist_dict[val] = count

                # save wabitplane_hist
                df_hist = pd.DataFrame(list(wa_hist_dict.items()), columns = ['val', 'count'])
                # wabitplane hist
                if os.path.isfile(wabitplane_hist):
                    print(f'[{self.layer_idx}]Update wabitplane_hist for w:{wbit}/a:{abit} ({wabitplane_hist})')
                    df_wabitplane_hist = pd.read_pickle(wabitplane_hist) 
                    df_merge = pd.merge(df_wabitplane_hist, df_hist, how="outer", on="val")
                    df_merge = df_merge.replace(np.nan, 0)
                    df_merge['count'] = df_merge['count_x'] + df_merge['count_y']
                    df_merge = df_merge[['val', 'count']]
                    df_merge.to_pickle(wabitplane_hist)
                else:
                    print(f'[{self.layer_idx}]Create wabitplane_hist for w:{wbit}/a:{abit} ({wabitplane_hist})')
                    df_hist.to_pickle(wabitplane_hist)

                # split output merge
                output_chunk = (out_tmp*a_mag).chunk(self.split_groups, dim=1)
                import pdb; pdb.set_trace()
                for g in range(0, self.split_groups):
                    if g==0:
                        out_tmp = output_chunk[g]
                    else:
                        out_tmp += output_chunk[g]

                bitplane_idx += 1

            # save abitplane_hist
            df_hist = pd.DataFrame(list(a_hist_dict.items()), columns = ['val', 'count'])
            # wbitplane hist
            if os.path.isfile(abitplane_hist):
                print(f'[{self.layer_idx}]Update abitplane_hist for a:{abit} ({abitplane_hist})')
                df_abitplane_hist = pd.read_pickle(abitplane_hist) 
                df_merge = pd.merge(df_abitplane_hist, df_hist, how="outer", on="val")
                df_merge = df_merge.replace(np.nan, 0)
                df_merge['count'] = df_merge['count_x'] + df_merge['count_y']
                df_merge = df_merge[['val', 'count']]
                df_merge.to_pickle(abitplane_hist)
            else:
                print(f'[{self.layer_idx}]Create abitplane_hist for a:{abit} ({abitplane_hist})')
                df_hist.to_pickle(abitplane_hist)

            # update layer hist
            for val, count in a_hist_dict.items():
                if val in layer_hist_dict.keys():
                    layer_hist_dict[val] += count
                else:
                    layer_hist_dict[val] = count
            
            output = out_tmp if abit == 0 else output+out_tmp

        # restore output's scale
        output = output * psum_scale

        # add bias
        if self.bias is not None:
            output += self.bias

        # save logger
        df.to_pickle(logger)
        # df_scaled.to_pickle(logger_scaled)

        # save hist
        df_hist = pd.DataFrame(list(layer_hist_dict.items()), columns = ['val', 'count'])
        # layer hist
        if os.path.isfile(layer_hist):
            print(f'[{self.layer_idx}] Update layer_hist ({layer_hist})')
            df_layer_hist = pd.read_pickle(layer_hist) 
            df_merge = pd.merge(df_layer_hist, df_hist, how="outer", on="val")
            df_merge = df_merge.replace(np.nan, 0)
            df_merge['count'] = df_merge['count_x'] + df_merge['count_y']
            df_merge = df_merge[['val', 'count']]
            df_merge.to_pickle(layer_hist)
        else:
            print(f'[{self.layer_idx}] Create layer_hist ({layer_hist})')
            df_hist.to_pickle(layer_hist)
        # network hist
        if os.path.isfile(network_hist):
            print(f'[{self.layer_idx}]Update network_hist ({network_hist})')
            df_network_hist = pd.read_pickle(network_hist) 
            df_merge = pd.merge(df_network_hist, df_hist, how="outer", on="val")
            df_merge = df_merge.replace(np.nan, 0)
            df_merge['count'] = df_merge['count_x'] + df_merge['count_y']
            df_merge = df_merge[['val', 'count']]
            df_merge.to_pickle(network_hist)
        else:
            print(f'[{self.layer_idx}] Create network_hist ({network_hist})')
            df_hist.to_pickle(network_hist)

        # output_real = F.linear(input, qweight, bias=None)
        # import pdb; pdb.set_trace()

        return output
    
    def _ADC_clamp_value(self):
        # get ADC clipping value for hist [Layer or Network hist]
        if self.pclipmode == 'layer':
            phist = f'{self.checkpoint}/hist/layer{self.layer_idx}_hist.pkl'
            # phist = f'./hist/layer{self.layer_idx}_hist.pkl'
        elif self.pclipmode == 'network':
            phist = f'{self.checkpoint}/hist/network_hist.pkl'

        if os.path.isfile(phist):
            # print(f'Load ADC_hist ({phist})')
            df_hist = pd.read_pickle(phist)
            mean, std, min, max = get_statistics_from_hist(df_hist)
        else:
            if self.pbits != 32:
                assert False, "Error: Don't have ADC hist file"
            else:
                mean, std, min, max = 0, 0, 0, 0
            
        # Why abs(mean) is used not mean?? => Asymmetric quantizaion is occured
        if self.pbits == 32:
            maxVal = 1
            minVal = 0
        else:
            if self.pclip == 'max':
                maxVal = max
                minVal = min
            else:
                maxVal =  (abs(mean) + self.psigma*std).round() 
                minVal = (abs(mean) - self.psigma*std).round() 
                if (self.mapping_mode == 'two_com') or (self.mapping_mode == 'ref_d') or (self.mapping_mode == 'PN'):
                    minVal = min if minVal < 0 else minVal
        
        midVal = (maxVal + minVal) / 2
        
        if self.info_print:
            write_file = f'{self.checkpoint}/Layer_clipping_range.txt'
            if os.path.isfile(write_file) and (self.layer_idx == 0):
                option = 'w'
            else:
                option = 'a'
            with open(write_file, option) as file:
                if self.layer_idx == 0:
                    file.write(f'{self.pclipmode}-wise Mode Psum quantization \n')
                    file.write(f'Layer_information  Mean    Std     Min Max Clip_Min    Clip_Max    Mid \n')
                file.write(f'Layer{self.layer_idx}  {mean}  {std}   {min}   {max}   {minVal}    {maxVal}    {midVal}\n')
            
            print(f'{self.pclipmode}-wise Mode Psum quantization')
            if self.pbits == 32:
                print(f'Layer{self.layer_idx} information | pbits {self.pbits}')
            else:
                print(f'Layer{self.layer_idx} information | pbits {self.pbits} | Mean: {mean} | Std: {std} | Min: {min} | Max: {max} | Clip Min: {minVal} | Clip Max: {maxVal} | Mid: {midVal}')
            self.info_print = False

        return minVal, maxVal, midVal

    def _bitserial_comp_forward(self, input):

        # get quantization parameter and input bitserial 
        bits = self.bits
        qweight = self.quant_func(self.weight, self.training)

        if self.wbit_serial:
            with torch.no_grad():
                w_scale = self.quant_func.get_alpha()
                sinput, a_scale = self.bitserial_act_split(input)

                psum_scale = w_scale * a_scale

                if self.psum_mode == 'sigma':
                    minVal, maxVal, midVal = self._ADC_clamp_value()
                    self.setting_pquant_func(pbits=self.pbits, center=minVal, pbound=midVal-minVal)
                elif self.psum_mode == 'scan':
                    pass
                else:
                    assert False, 'This script does not support {self.psum_mode}'

                ### in-mem computation mimic (split conv & psum quant/merge)
                input_chunk = torch.chunk(sinput, bits, dim=1)
                weight_chunk = torch.chunk(qweight/w_scale, 1, dim=1)

                # to compare output data
                for abit, input_s in enumerate(input_chunk):
                    out_adc = None
                    for wbit, weight_s in enumerate(weight_chunk):
                        out_tmp = self._split_forward(input_s, weight_s, ignore_bias=True, cat_output=False, infer_only=True)

                        a_mag = 2**(abit)
                        out_adc = psum_quant_merge(out_adc, out_tmp,
                                                    pbits=self.pbits, step=self.pstep, 
                                                    half_num_levels=self.phalf_num_levels, 
                                                    pbound=self.pbound, center=self.center, weight=a_mag,
                                                    groups=self.split_groups, pzero=self.pzero)

                    output = out_adc if abit == 0 else output+out_adc

                # restore output's scale
                output = output * psum_scale
        else:
            # no serial compuatation with psum computation 
            self.pbits = 32
            output = self._split_forward(input, qweight, ignore_bias=True, merge_group=True)

        # add bias
        if self.bias is not None:
            output += self.bias
        
        # output_real = F.linear(input, qweight, bias=None)
        # import pdb; pdb.set_trace()

        return output

    def forward(self, x):
        if self.act_func is not None:
            x = self.act_func(x)
        
        if self.bitserial_log:
            return self._bitserial_log_forward(x)
        else:
            return self._bitserial_comp_forward(x)

    def extra_repr(self):
        """Provides layer information, including wbits, when print(model) is called."""
        s =  'in_features={}, out_features={}, bias={}, bits={}, wbit_serial={}, split_groups={}, '\
            'mapping_mode={}, cbits={}, psum_mode={}, pbits={}, pbound={}, '\
            'bitserial_log={}, layer_idx={}'\
            .format(self.in_features, self.out_features, self.bias is not None, self.bits, self.wbit_serial,
            self.split_groups, self.mapping_mode, self.cbits, self.psum_mode, self.pbits, self.pbound, 
            self.bitserial_log, self.layer_idx)
        return s

def get_statistics_from_hist(df_hist):
    num_elements = df_hist['count'].sum()
    # min/max
    min_val = df_hist['val'].min()
    max_val = df_hist['val'].max()

    # mean
    df_hist['sum'] = df_hist['val'] * df_hist['count']
    mean_val = df_hist['sum'].sum() / num_elements

    # std
    df_hist['centered'] = df_hist['val'] - mean_val
    df_hist['var_sum'] = (df_hist['centered'] * df_hist['centered']) * df_hist['count']
    var_val = df_hist['var_sum'].sum() / num_elements
    std_val = math.sqrt(var_val)

    return [mean_val, std_val, min_val, max_val] 

def psum_initialize(model, act=True, weight=True, fixed_bit=-1, cbits=4, arraySize=128, mapping_mode='2T2R', psum_mode='sigma',
                    wbit_serial=False, pbits=32, pclipmode='layer', pclip='sigma', psigma=3, pbound=None, center=None,
                    checkpoint=None, log_file=None):
    counter=0
    for name, module in model.named_modules():
        if isinstance(module, (QuantActs.ReLU, QuantActs.HSwish, QuantActs.Sym)) and act:
            module.quant = True

            module.quant_func.noise = False
            module.quant_func.is_stochastic = True
            module.quant_func.is_discretize = True

            if fixed_bit != -1 :
                bit = ( fixed_bit+0.00001 -2 ) / 12
                bit = np.log(bit/(1-bit))
                module.quant_func.bit.data.fill_(bit)
                module.quant_func.bit.requires_grad = False
            
            #module.bit.data.fill_(-2)

        if isinstance(module, (Psum_QConv2d, Psum_QLinear))and weight:
            module.quant_func.noise = False
            module.quant_func.is_stochastic = True
            module.quant_func.is_discretize = True

            if fixed_bit != -1 :
                bit = ( fixed_bit -2 ) / 12
                bit = np.log(bit/(1-bit))
                module.quant_func.bit.data.fill_(bit)
                module.quant_func.bit.requires_grad = False
                module.bits = fixed_bit
            
            module.cbits = cbits
            module.wbit_serial = wbit_serial
            module.model_split_groups(arraySize)
            module.mapping_mode = mapping_mode
            module.pbits = pbits
            module.pclipmode = pclipmode.lower()
            module.psum_mode = psum_mode
            if psum_mode == 'sigma':
                module.pclip = pclip
                module.psigma = psigma
            elif psum_mode == 'scan':
                module.setting_pquant_func(pbits, center, pbound)
            else:
                assert False, "Only two options [sigma, scan]"
            
            module.bitserial_log = log_file
            module.layer_idx = counter 
            module.checkpoint = checkpoint
            counter += 1

def unset_bitserial_log(model):
    print("start unsetting Bitserial layers log bitplane info")
    counter = 0
    for name, module in model.named_modules():
        if isinstance(module, (Psum_QConv2d, Psum_QLinear)):
            module.bitserial_log = False
            print("Finish log unsetting {}, idx: {}".format(name.replace("module.", ""), counter))
            counter += 1

def hnoise_initilaize(model, weight=False, hnoise=True, cbits=4, mapping_mode=None, co_noise=0.01, noise_type='prop', res_val='rel', max_epoch=-1):
    for name, module in model.named_modules():
        if isinstance(module, (Psum_QConv2d, Psum_QLinear)) and weight and hnoise:
            module.quant_func.hnoise = True

            if noise_type == 'grad':
                assert max_epoch != -1, "Enter max_epoch in hnoise_initialize function"
            if hnoise:
                module.quant_func.hnoise_init(cbits=cbits, mapping_mode=mapping_mode, co_noise=co_noise, noise_type=noise_type, res_val=res_val, max_epoch=max_epoch)

class PsumQuantOps(object):
    psum_initialize = psum_initialize
    hnoise_initilaize = hnoise_initilaize
    unset_bitserial_log = unset_bitserial_log
    Conv2d = Psum_QConv2d
    Linear = Psum_QLinear