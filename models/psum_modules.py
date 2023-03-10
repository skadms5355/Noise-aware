from asyncio.proactor_events import _ProactorDuplexPipeTransport
from signal import Sigmasks
import numpy as np
from torch.autograd import Variable
import torch
import math
import os
import torch.nn.functional as F
import torch.nn as nn
from torch.nn.parameter import Parameter 
import pandas as pd
from utils.cell_graph import cell_graph
from .noise_cell import Noise_Cell
import utils.padding as Pad
from .quantized_lsq_modules import *
from .quantized_basic_modules import *
from .bitserial_modules import *
from .split_modules import *
# custom kernel
import conv_sweight_cuda

# split convolution across input channel
def split_conv(weight, nWL):
    nIC = weight.shape[1]
    nWH = weight.shape[2]*weight.shape[3]
    nMem = int(math.ceil(nIC/math.floor(nWL/nWH)))
    nIC_list = [int(math.floor(nIC/nMem)) for _ in range(nMem)]
    for idx in range((nIC-nIC_list[0]*nMem)):
        nIC_list[idx] += 1

    return nIC_list

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

class PsumQConv(SplitConv):
    """
        Quant(LSQ)Conv + Psum quantization
    """
    def __init__(self, in_channels, out_channels, wbits=32, kernel_size=3, stride=1, padding=0, groups=1, symmetric=False, bias=False,
                padding_mode='zeros', arraySize=128, wbit_serial=False, mapping_mode='none', psum_mode='sigma', cbits=None, 
                is_noise=False, noise_type=None):
        super(PsumQConv, self).__init__(in_channels, out_channels, kernel_size,
                                       stride=stride, padding=padding, groups=groups, bias=bias)
        # for Qconv
        self.wbits = wbits
        self.wbit_serial = wbit_serial
        self.padding = padding
        self.stride = stride
        self.padding_mode = padding_mode
        
        if self.padding_mode == 'zeros':
            self.padding_value = 0
        elif self.padding_mode == 'ones':
            self.padding_value = 1
        elif self.padding_mode == 'alter':
            self.padding_value = 0

        self.quan_w_fn = LSQReturnScale(bit=self.wbits, half_range=False, symmetric=symmetric, per_channel=False)

        # for split
        # self.split_nIC = split_conv(self.weight, arraySize)
        self.split_groups = calculate_groups(arraySize, self.fan_in)
        if self.fan_in % self.split_groups != 0:
            raise ValueError('fan_in must be divisible by groups')
        self.group_fan_in = int(self.fan_in / self.split_groups)
        self.group_in_channels = int(np.ceil(in_channels / self.split_groups))
        residual = self.group_fan_in % self.kSpatial
        if residual != 0:
            if self.kSpatial % residual != 0:
                self.group_in_channels += 1
        ## log move group for masking & group convolution
        self.group_move_in_channels = torch.zeros(self.split_groups-1, dtype=torch.int)
        group_in_offset = torch.zeros(self.split_groups, dtype=torch.int)
        self.register_buffer('group_in_offset', group_in_offset)
        ## get group conv info
        self._group_move_offset()
        
        # sweight
        sweight = torch.Tensor(self.out_channels*self.split_groups, self.group_in_channels, kernel_size, kernel_size)
        self.register_buffer('sweight', sweight)

        # for psum quantization
        self.mapping_mode = mapping_mode # Array mapping method [none, 2T2R, two_com, ref_d]]
        self.cbits = cbits # Cell bits [multi, binary]
        self.psum_mode = psum_mode
        self.pclipmode = 'Layer'
        self.pbits = 32
        # for scan version
        self.pstep = None
        self.pzero = None  # contain zero value (True)
        self.center = None
        self.pbound = None
        # for sigma version
        self.pclip = 'sigma'
        self.psigma = 3

        # for noise option
        self.is_noise = is_noise
        self.noise_type = noise_type
        self.noise_param = 0
        self.ratio = 100
        if self.is_noise:
            self.noise_cell = Noise_Cell(self.wbits, self.cbits, self.mapping_mode, self.noise_type)

        # for logging
        self.bitserial_log = False
        self.layer_idx = -1
        self.checkpoint = None
        self.info_print = True
        self.graph_path = None
        self.weight_dist = False

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

    def _weight_bitserial(self, weight, w_scale, cbits=4):
        weight_uint = (weight / w_scale).round_()
        if self.mapping_mode == "two_com":
            signed_w = (2**(self.wbits-1)-1)*torch.where(weight_uint<0, 1.0, 0.0) # same as maximum cell state
            value_w = torch.where(weight_uint<0, 2**(self.wbits-1) - abs(weight_uint), weight_uint)
            if cbits == 1:
                weight_serial = bitserial_func(value_w, (self.wbits-1))
                output = torch.cat((weight_serial, signed_w), 1)
                split_num = self.wbits
            elif cbits > 1:
                output = torch.cat((value_w, signed_w), 1)
                split_num = 2
            else:
                assert None, "Please select cell state mode"
        elif self.mapping_mode == "ref_d":
            if cbits > 1:
                shift_v = 2**(self.wbits-1)
                shift_w = shift_v*torch.ones(weight.size()).cuda()
                value_w = torch.add(weight_uint, shift_v)
                output = torch.cat((value_w, shift_w), 1)
                split_num = 2
            else:
                assert False, "Pleas select multi cell state for reference digital mapping mode"
        elif self.mapping_mode == "2T2R":
            if self.is_noise:
                zeros = torch.zeros(weight_uint.size(), device=weight_uint.device)
                pos_w = torch.where(weight_uint>0, weight_uint, zeros)
                neg_w = torch.where(weight_uint<0, abs(weight_uint), zeros)
                output = torch.cat((pos_w, neg_w), 1)# 9 level cell bits 
                split_num=2
            else:
                # range = 2 ** (self.wbits - 1) - 1
                # output = torch.clamp(weight_uint, -range, range) # 8 level cell bits 
                output = weight_uint # 9 level cell bits 
                split_num=1
        elif self.mapping_mode == "ref_a":
            if self.is_noise:
                shift_v = 2**(self.wbits-1)
                shift_w = shift_v*torch.ones(weight.size()).cuda()
                value_w = torch.add(weight_uint, shift_v)
                output = torch.cat((value_w, shift_w), 1)
                split_num = 2
            else:
                output = weight_uint # 9 level cell bits 
                split_num=1
        elif self.mapping_mode == "PN":
            if cbits > 1:
                zeros = torch.zeros(weight_uint.size(), device=weight_uint.device)
                pos_w = torch.where(weight_uint>0, weight_uint, zeros)
                neg_w = torch.where(weight_uint<0, abs(weight_uint), zeros)
                output = torch.cat((pos_w, neg_w), 1)# 9 level cell bits 
                split_num=2
            else:
                assert False, "Pleas select multi cell state for reference digital mapping mode"
        else:
            output = weight_uint
            split_num = 1

        return output, split_num  

    # store weight magnitude for in-mem computing mimic 
    ## Assume that cell bits are enough
    def _output_magnitude(self, abit, wbit, split_num):
        multi_scale = 1
        if self.mapping_mode == "two_com":
            w_mag = 2**(self.wbits-1) if (wbit+1)==split_num else 2**wbit
            if self.cbits > 1:
                multi_scale = 2**(self.wbits-1)-1 if (wbit+1)==split_num else 1
        else:
            w_mag = 1

        a_mag = int((2**abit))
        return a_mag, w_mag, multi_scale
    
    def _cell_noise_inject(self, weight_list):
        weight_cond = []
        self.noise_cell.update_setting(self.noise_param, self.ratio)

        for weight in weight_list:
            weight_cond.append(self.noise_cell(weight))
        
        return weight_cond
    
    def _bitserial_log_forward(self, input):
        print(f'[layer{self.layer_idx}]: bitserial mac log')
        # delete padding_shpe & additional padding operation by matching padding/stride format with nn.Conv2d
        if self.padding > 0:
            padding_shape = (self.padding, self.padding, self.padding, self.padding)
            input = Pad.pad(input, padding_shape, self.padding_mode, self.padding_value)

        # local parameter setting
        bitplane_idx = 0

        # get quantization parameter and input bitserial 
        qweight, w_scale = self.quan_w_fn(self.weight)
        sinput, a_scale, abits = Bitserial.bitserial_act(input, debug=False)
        
        ## get dataframe
        logger = f'{self.checkpoint}/layer{self.layer_idx}_mac_static.pkl'
        df = pd.DataFrame(columns=['wbits', 'abits', 'mean', 'std', 'min', 'max'])

        # logger_scaled = f'{self.checkpoint}/layer{self.layer_idx}_wabitplane_mac_static_scaled_lastbatch.pkl'
        # df_scaled = pd.DataFrame(columns=['wbits', 'abits', 'mean', 'std', 'min', 'max'])

        layer_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_hist.pkl'
        network_hist = f'{self.checkpoint}/hist/network_hist.pkl'

        #plane hist
        
        ### in-mem computation mimic (split conv & psum quant/merge)
        input_chunk = torch.chunk(sinput, abits, dim=1)
        self.sweight = conv_sweight_cuda.forward(self.sweight, qweight, self.group_in_offset, self.split_groups)
        sweight, wsplit_num = self._weight_bitserial(self.sweight, w_scale, cbits=self.cbits)
        weight_chunk = torch.chunk(sweight, wsplit_num, dim=1)

        ### Cell noise injection + Cell conductance value change
        if self.is_noise:
            # weight_chunk_debug= weight_chunk
            # print(set(weight_chunk_debug[0].cpu().detach().numpy().ravel()))
            weight_chunk = self._cell_noise_inject(weight_chunk)
            delta_G = self.noise_cell.get_deltaG()

        psum_scale = w_scale * a_scale 
        out_tmp = None
        layer_hist_dict = {}
        for abit, input_s in enumerate(input_chunk):
            abitplane_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_a:{abit}_hist.pkl'
            a_hist_dict = {}
            for wbit, weight_s in enumerate(weight_chunk):
                wabitplane_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_w:{wbit}_a:{abit}_hist.pkl'
                wa_hist_dict = {}
                out_tmp = self._split_forward(input_s, weight_s, padded=True, ignore_bias=True,
                                                weight_is_split=True, infer_only=True) 
                # out_tmp = F.conv2d(input_s[:,nIC_cnt:nIC_cnt+self.split_nIC[idx],:,:], weight_s, bias=self.bias,
                #             stride=self.stride, dilation=self.dilation, groups=self.groups)

                if self.is_noise:
                    if (self.mapping_mode=='2T2R') or (self.mapping_mode=='ref_a'):
                        if wbit == 0:
                            temp = out_tmp
                            continue
                        else:
                            out_tmp = (temp - out_tmp) / delta_G
                    else:
                        out_tmp /= delta_G

                a_mag, w_mag, cell_scale = self._output_magnitude(abit, wbit, wsplit_num)
                out_array = (out_tmp/a_mag).round() # noise bound set to round function
                ## NOTE
                df.loc[bitplane_idx] = [wbit, abit,
                                                float(out_array.mean()), 
                                                float(out_array.std()), 
                                                float(out_array.min()), 
                                                float(out_array.max())] 

                # out_tmp_scale = out_tmp / self.pquant_bitplane[bitplane_idx]
                out_min = out_array.min()
                out_max = out_array.max()

                # df_scaled.loc[bitplane_idx] = [wbit, abit,
                #                                 float(out_tmp_scale.mean()), 
                #                                 float(out_tmp_scale.std()), 
                #                                 float(out_min), 
                #                                 float(out_max)] 

                # update hist
                for val in range(int(out_min), int(out_max)+1):
                    count = out_array.eq(val).sum().item()
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
                output_chunk = out_tmp.chunk(self.split_groups, dim=1) 
                for g in range(0, self.split_groups):
                    if g==0:
                        out_tmp = output_chunk[g]
                    else:
                        out_tmp += output_chunk[g]

                # weight output summation
                if self.mapping_mode == 'two_com':
                    if wsplit_num == wbit+1:
                        out_wsum -= out_tmp * w_mag / cell_scale
                    else:
                        out_wsum = out_tmp if wbit == 0 else out_wsum + out_tmp
                elif (self.mapping_mode == 'ref_d') or (self.mapping_mode == 'PN'):
                    out_wsum = out_tmp if wbit == 0 else out_wsum - out_tmp
                else:
                    # out_wsum = out_tmp if wbit == 0 else out_wsum + out_tmp
                    out_wsum = out_tmp 

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
            
            output = out_wsum if abit ==0 else output+out_wsum

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
        if self.pclipmode == 'Layer':
            phist = f'{self.checkpoint}/hist/layer{self.layer_idx}_hist.pkl'
            # phist = f'./hist/layer{self.layer_idx}_hist.pkl'
        elif self.pclipmode == 'Network':
            phist = f'{self.checkpoint}/hist/network_hist.pkl'

        if os.path.isfile(phist):
            # print(f'Load ADC_hist ({phist})')
            df_hist = pd.read_pickle(phist)
            mean, std, min, max = get_statistics_from_hist(df_hist)
        else:
            if self.pbits != 32:
                assert False, f"Error: Don't have ADC hist in {phist} file"
            else:
                mean, std, min, max = 0, 1, 0, 0

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
        qweight, w_scale = self.quan_w_fn(self.weight)

        if self.wbit_serial:
            with torch.no_grad():
                sinput, a_scale, abits = Bitserial.bitserial_act(input, debug=False)

                if self.psum_mode == 'sigma':
                    minVal, maxVal, midVal = self._ADC_clamp_value()
                    self.setting_pquant_func(pbits=self.pbits, center=minVal, pbound=midVal-minVal)
                elif self.psum_mode == 'scan':
                    pass
                else:
                    assert False, 'This script does not support {self.psum_mode}'
                ### in-mem computation mimic (split conv & psum quant/merge)
                input_chunk = torch.chunk(sinput, abits, dim=1)
                self.sweight = conv_sweight_cuda.forward(self.sweight, qweight, self.group_in_offset, self.split_groups)
                sweight, wsplit_num = self._weight_bitserial(self.sweight, w_scale, cbits=self.cbits)
                weight_chunk = torch.chunk(sweight, wsplit_num, dim=1)

                if self.weight_dist:
                    cell_graph(weight_chunk, wsplit_num, self.graph_path, self.layer_idx, self.mapping_mode, self.wbits, self.cbits)                    
                    self.weight_dist = False

                ### Cell noise injection + Cell conductance value change
                if self.is_noise:
                    weight_chunk = self._cell_noise_inject(weight_chunk)
                    delta_G = self.noise_cell.get_deltaG()

                psum_scale = w_scale * a_scale
                out_adc = None
                for abit, input_s in enumerate(input_chunk):
                    for wbit, weight_s in enumerate(weight_chunk):
                        out_tmp = self._split_forward(input_s, weight_s, padded=True, ignore_bias=True, cat_output=True,
                                                weight_is_split=True, infer_only=True)
                        if self.is_noise:
                            if (self.mapping_mode=='2T2R') or (self.mapping_mode=='ref_a'):
                                if wbit == 0:
                                    temp = out_tmp
                                    continue
                                else:
                                    out_tmp = (temp - out_tmp) / delta_G

                                    # out_tmp = list(map(lambda x: x/delta_G, temp))
                            else:
                                out_tmp /= delta_G
                                # out_tmp = list(map(lambda x: x/delta_G, out_tmp))
                        out_tmp = torch.chunk(out_tmp, self.split_groups, dim=1)
                        out_tmp = list(map(lambda x: x.contiguous(), out_tmp))

                        a_mag, w_mag, cell_scale = self._output_magnitude(abit, wbit, wsplit_num)
                        out_adc = psum_quant_merge(out_adc, out_tmp,
                                                    pbits=self.pbits, step=self.pstep, 
                                                    half_num_levels=self.phalf_num_levels, 
                                                    pbound=self.pbound, center=self.center, weight=a_mag,
                                                    groups=self.split_groups, pzero=self.pzero)

                        # weight output summation
                        if self.mapping_mode == 'two_com':
                            if wsplit_num == wbit+1:
                                out_wsum -= out_adc * w_mag / cell_scale
                            else:
                                out_wsum = out_adc if wbit == 0 else out_wsum + out_adc
                        elif (self.mapping_mode == 'ref_d') or (self.mapping_mode == 'PN'):
                            out_wsum = out_adc if wbit == 0 else out_wsum - out_adc
                        else:
                            # out_wsum = out_adc if wbit == 0 else out_wsum + out_adc
                            out_wsum = out_adc
                        out_adc = None
                    output = out_wsum if abit == 0 else output+out_wsum

                # restore output's scale
                output = output * psum_scale
        else:
            abit_serial = Bitserial.abit_serial()
            if not abit_serial:
                # in-mem computation mimic (split conv & psum quant/merge)
                self.pbits = 32
                output = self._split_forward(input, qweight, padded=True, ignore_bias=True, merge_group=True)

                if self.training:
                    with torch.no_grad():
                        sweight, wsplit_num = self._weight_bitserial(qweight, w_scale, cbits=self.cbits)
                        weight_chunk = torch.chunk(sweight, wsplit_num, dim=1)
                        if self.is_noise:
                            self.noise_cell.update_setting(self.noise_param, self.ratio)
                            weight_chunk = self._cell_noise_inject(weight_chunk)
                            delta_G = self.noise_cell.get_deltaG()

                        for wbit, weight_s in enumerate(weight_chunk):
                            out_tmp = self._split_forward(input, weight_s.contiguous(), padded=True, ignore_bias=True, infer_only=True, merge_group=True)
                            _, w_mag, cell_scale = self._output_magnitude(abit=0, wbit=wbit, split_num=wsplit_num)                 

                            if self.is_noise:
                                if (self.mapping_mode=='2T2R') or (self.mapping_mode=='ref_a'):
                                    if wbit == 0:
                                        temp = out_tmp
                                        continue
                                    else:
                                        out_tmp = (temp - out_tmp) / delta_G
                                else:
                                    out_tmp /= delta_G

                            # weight output summation
                            if self.mapping_mode == 'two_com':
                                if wsplit_num == wbit+1:
                                    out_wsum -= out_tmp * w_mag / cell_scale
                                else:
                                    out_wsum = out_tmp if wbit == 0 else out_wsum + out_tmp
                            elif (self.mapping_mode == 'ref_d') or (self.mapping_mode == 'PN'):
                                out_wsum = out_tmp if wbit == 0 else out_wsum - out_tmp
                            else:
                                # out_wsum = out_adc if wbit == 0 else out_wsum + out_adc
                                out_wsum = out_tmp

                        output = out_wsum * w_scale
            else:
                assert False, "we do not support act serial only model"

        # add bias
        if self.bias is not None:
            output += self.bias

        # output_real = F.conv2d(input, qweight, bias=self.bias,
        #                         stride=self.stride, dilation=self.dilation, groups=self.groups)
        # import pdb; pdb.set_trace()

        return output

    def forward(self, input):
        if self.bitserial_log:
            return self._bitserial_log_forward(input)
        else:
            if not self.wbit_serial and not self.is_noise and self.wbits==32:
                return F.conv2d(input, self.weight, bias=self.bias,
                        stride=self.stride, padding=self.padding, dilation=self.dilation, groups=self.groups)
            else:
                return self._bitserial_comp_forward(input)

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
        s += ', wbits={wbits}, wbit_serial={wbit_serial}'
        s += ', split_groups={split_groups}, mapping_mode={mapping_mode}, cbits={cbits}'
        s += ', psum_mode={psum_mode}, pbits={pbits}, pbound={pbound}'
        s += ', noise={is_noise}, noise_type={noise_type}, noise_param={noise_param}, cell_ratio={ratio}'
        s += ', bitserial_log={bitserial_log}, layer_idx={layer_idx}'            
        return s.format(**self.__dict__)

class PsumQLinear(SplitLinear):
    """
        Quant(LSQ)Linear + Psum quantization
    """
    def __init__(self, in_features, out_features, wbits, symmetric=False, bias=False,
                arraySize=128, wbit_serial=False, mapping_mode='none', psum_mode='sigma', cbits=None,
                is_noise=False, noise_type=None):
        super(PsumQLinear, self).__init__(in_features, out_features, bias=bias)
        # for QLinear
        self.wbits = wbits
        self.wbit_serial = wbit_serial

        self.quan_w_fn = LSQReturnScale(bit=self.wbits, half_range=False, symmetric=symmetric, per_channel=False)

        # for split
        # self.split_nIF = split_linear(self.weight, arraySize)
        self.split_groups = calculate_groups(arraySize, in_features)
        if in_features % self.split_groups != 0:
            raise ValueError('in_features must be divisible by groups')
        self.group_in_features = int(in_features / self.split_groups)

        # for psum quantization
        self.mapping_mode = mapping_mode # Array mapping method [none, 2T2R, two_com, ref_d]
        self.cbits = cbits # Cell bits [multi, binary]
        self.psum_mode = psum_mode
        self.pclipmode = 'Layer'
        self.pbits = 32
        # for scan version
        self.pstep = None
        self.pzero = None # contain zero value (True)
        self.center = None
        self.pbound = arraySize if arraySize > 0 else self.fan_in
        # for sigma version
        self.pclip = 'sigma'
        self.psigma = 3

        # for noise option
        self.is_noise = is_noise
        self.noise_type = noise_type
        self.noise_param = 0
        self.ratio = 100
        if self.is_noise:
            self.noise_cell = Noise_Cell(self.wbits, self.cbits, self.mapping_mode, self.noise_type)

        # for logging
        self.bitserial_log = False
        self.layer_idx = -1
        self.checkpoint = None
        self.info_print = True
        self.graph_path = None
        self.weight_dist = False
    
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
    
    def _weight_bitserial(self, weight, w_scale, cbits=1):
        weight_uint = (weight / w_scale).round_()
        if self.mapping_mode == "two_com":
            signed_w = (2**(self.wbits-1)-1)*torch.where(weight_uint<0, 1.0, 0.0) # same as maximum cell state
            value_w = torch.where(weight_uint<0, 2**(self.wbits-1) - abs(weight_uint), weight_uint)
            if cbits == 1:
                weight_serial = bitserial_func(value_w, (self.wbits-1))
                output = torch.cat((weight_serial, signed_w), 1)
                split_num = self.wbits
            elif cbits > 1:
                output = torch.cat((value_w, signed_w), 1)
                split_num = 2
            else:
                assert None, "Please select cell state mode"
        elif self.mapping_mode == "ref_d":
            if cbits > 1:
                shift_v = 2**(self.wbits-1)
                shift_w = shift_v*torch.ones(weight.size()).cuda()
                value_w = torch.add(weight_uint, shift_v)
                output = torch.cat((value_w, shift_w), 1)
                split_num = 2
            else:
                assert False, "Pleas select multi cell state for reference digital mapping mode"
        elif self.mapping_mode == "2T2R":
            if self.is_noise:
                zeros = torch.zeros(weight_uint.size(), device=weight_uint.device)
                pos_w = torch.where(weight_uint>0, weight_uint, zeros)
                neg_w = torch.where(weight_uint<0, abs(weight_uint), zeros)
                output = torch.cat((pos_w, neg_w), 1)# 9 level cell bits 
                split_num=2
            else:
                # range = 2 ** (self.wbits - 1) - 1
                # output = torch.clamp(weight_uint, -range, range) # 8 level cell bits 
                output = weight_uint # 9 level cell bits 
                split_num=1
        elif self.mapping_mode == "PN":
            if cbits > 1:
                zeros = torch.zeros(weight_uint.size(), device=weight_uint.device)
                pos_w = torch.where(weight_uint>0, weight_uint, zeros)
                neg_w = torch.where(weight_uint<0, abs(weight_uint), zeros)
                output = torch.cat((pos_w, neg_w), 1)# 9 level cell bits 
                split_num=2
            else:
                assert False, "Pleas select multi cell state for reference digital mapping mode"
        elif self.mapping_mode == "ref_a":
            if self.is_noise:
                shift_v = 2**(self.wbits-1)
                shift_w = shift_v*torch.ones(weight.size()).cuda()
                value_w = torch.add(weight_uint, shift_v)
                output = torch.cat((value_w, shift_w), 1)
                split_num = 2
            else:
                output = weight_uint # 9 level cell bits 
                split_num=1
        else:
            output = weight_uint
            split_num = 1

        return output, split_num 

    # store weight magnitude for in-mem computing mimic 
    ## Assume that cell bits are enough
    def _output_magnitude(self, abit, wbit, split_num):
        multi_scale = 1
        if self.mapping_mode == "two_com":
            w_mag = 2**(self.wbits-1) if (wbit+1)==split_num else 2**wbit
            if self.cbits > 1:
                multi_scale = 2**(self.wbits-1)-1 if (wbit+1)==split_num else 1
        else:
            w_mag = 1

        a_mag = int((2**abit))
        return a_mag, w_mag, multi_scale

    def _cell_noise_inject(self, weight_list):
        weight_cond = []
        self.noise_cell.update_setting(self.noise_param, self.ratio)

        for weight in weight_list:
            weight_cond.append(self.noise_cell(weight))
        
        return weight_cond


    def _bitserial_log_forward(self, input):
        print(f'[layer{self.layer_idx}]: bitserial mac log')

        # local parameter setting
        bitplane_idx = 0

        # get quantization parameter and input bitserial 
        qweight, w_scale = self.quan_w_fn(self.weight)
        sinput, a_scale, abits = Bitserial.bitserial_act(input, debug=False)
        psum_scale = w_scale * a_scale

        ## get dataframe
        logger = f'{self.checkpoint}/layer{self.layer_idx}_mac_static.pkl'
        df = pd.DataFrame(columns=['wbits', 'abits', 'mean', 'std', 'min', 'max'])

        # logger_scaled = f'{self.checkpoint}/layer{self.layer_idx}_wabitplane_mac_static_scaled_lastbatch.pkl'
        # df_scaled = pd.DataFrame(columns=['wbits', 'abits', 'mean', 'std', 'min', 'max'])

        layer_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_hist.pkl'
        network_hist = f'{self.checkpoint}/hist/network_hist.pkl'
        
        layer_hist_dict = {}
        ### in-mem computation mimic (split conv & psum quant/merge)
        input_chunk = torch.chunk(sinput, abits, dim=1)
        sweight, wsplit_num = self._weight_bitserial(qweight, w_scale, cbits=self.cbits)
        weight_chunk = torch.chunk(sweight, wsplit_num, dim=1)

        ### Cell conductance value change + Cell noise injection 
        if self.is_noise:
            # weight_chunk_debug= weight_chunk
            # print(set(weight_chunk_debug[0].cpu().detach().numpy().ravel()))
            weight_chunk = self._cell_noise_inject(weight_chunk)
            delta_G = self.noise_cell.get_deltaG()

        out_tmp = None
        for abit, input_s in enumerate(input_chunk):
            abitplane_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_a:{abit}_hist.pkl'
            a_hist_dict = {}
            for wbit, weight_s in enumerate(weight_chunk):
                wabitplane_hist = f'{self.checkpoint}/hist/layer{self.layer_idx}_w:{wbit}_a:{abit}_hist.pkl'
                wa_hist_dict = {}
                out_tmp = self._split_forward(input_s, weight_s, ignore_bias=True, infer_only=True)
                # out_tmp = F.linear(input_s[:,nIF_cnt:nIF_cnt+self.split_nIF[idx]], weight_s, bias=None)
                
                # noise operation
                if self.is_noise:
                    if (self.mapping_mode=='2T2R') or (self.mapping_mode=='ref_a'):
                        if wbit == 0:
                            temp = out_tmp
                            continue
                        else:
                            out_tmp = (temp - out_tmp) / delta_G
                    else:
                        out_tmp /= delta_G
                
                a_mag, w_mag, cell_scale = self._output_magnitude(abit, wbit, wsplit_num)
                out_array = (out_tmp/a_mag).round()
                                
                ## NOTE
                df.loc[bitplane_idx] = [wbit, abit,
                                                float(out_array.mean()), 
                                                float(out_array.std()), 
                                                float(out_array.min()), 
                                                float(out_array.max())] 

                # out_tmp_scale = out_tmp / self.pquant_bitplane[bitplane_idx]
                out_min = out_array.min()
                out_max = out_array.max()

                # df_scaled.loc[bitplane_idx] = [wbit, abit,
                #                                 float(out_tmp_scale.mean()), 
                #                                 float(out_tmp_scale.std()), 
                #                                 float(out_min), 
                #                                 float(out_max)] 

                # update hist
                for val in range(int(out_min), int(out_max)+1):
                    count = out_array.eq(val).sum().item()
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
                output_chunk = out_tmp.chunk(self.split_groups, dim=1) 
                for g in range(0, self.split_groups):
                    if g==0:
                        out_tmp = output_chunk[g]
                    else:
                        out_tmp += output_chunk[g]

                # weight output summation
                if self.mapping_mode == 'two_com':
                    if wsplit_num == wbit+1:
                        out_wsum -= out_tmp * w_mag / cell_scale
                    else:
                        out_wsum = out_tmp if wbit == 0 else out_wsum + out_tmp # Need revision (bitparallel check!)
                elif (self.mapping_mode == 'ref_d') or (self.mapping_mode == 'PN'):
                    out_wsum = out_tmp if wbit == 0 else out_wsum - out_tmp
                else:
                    # out_wsum = out_tmp if wbit == 0 else out_wsum + out_tmp
                    out_wsum = out_tmp 

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
            
            output = out_wsum if abit ==0 else output+out_wsum

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
        if self.pclipmode == 'Layer':
            phist = f'{self.checkpoint}/hist/layer{self.layer_idx}_hist.pkl'
            # phist = f'./hist/layer{self.layer_idx}_hist.pkl'
        elif self.pclipmode == 'Network':
            phist = f'{self.checkpoint}/hist/network_hist.pkl'

        if os.path.isfile(phist):
            # print(f'Load ADC_hist ({phist})')
            df_hist = pd.read_pickle(phist)
            mean, std, min, max = get_statistics_from_hist(df_hist)
        else:
            if self.pbits != 32:
                assert False, "Error: Don't have ADC hist file"
            else:
                mean, std, min, max = 0, 1, 0, 0
            
        # Why abs(mean) is used not mean?? => Asymmetric quantizaion is occured
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
            print(f'{self.pclipmode}-wise Mode Psum quantization')
            if self.pbits == 32:
                print(f'Layer{self.layer_idx} information | pbits {self.pbits}')
            else:
                print(f'Layer{self.layer_idx} information | pbits {self.pbits} | Mean: {mean} | Std: {std} | Min: {min} | Max: {max} | Clip Min: {minVal} | Clip Max: {maxVal} | Mid: {midVal}')
            self.info_print = False

        return minVal, maxVal, midVal
   
    def _bitserial_comp_forward(self, input):

        # get quantization parameter and input bitserial 
        qweight, w_scale = self.quan_w_fn(self.weight)

        if self.wbit_serial:
            with torch.no_grad():
                sinput, a_scale, abits = Bitserial.bitserial_act(input, debug=False)
                psum_scale = w_scale * a_scale

                if self.psum_mode == 'sigma':
                    minVal, maxVal, midVal = self._ADC_clamp_value()
                    self.setting_pquant_func(pbits=self.pbits, center=minVal, pbound=midVal-minVal)
                elif self.psum_mode == 'scan':
                    pass
                else:
                    assert False, 'This script does not support {self.psum_mode}'

                ### in-mem computation mimic (split conv & psum quant/merge)
                input_chunk = torch.chunk(sinput, abits, dim=1)
                sweight, wsplit_num = self._weight_bitserial(qweight, w_scale, cbits=self.cbits)
                weight_chunk = torch.chunk(sweight, wsplit_num, dim=1)

                if self.weight_dist:
                    cell_graph(weight_chunk, wsplit_num, self.graph_path, self.layer_idx, self.mapping_mode, self.wbits, self.cbits)
                    self.weight_dist = False

                ### Cell noise injection + Cell conductance value change
                if self.is_noise:
                    weight_chunk = self._cell_noise_inject(weight_chunk)
                    delta_G = self.noise_cell.get_deltaG()

                # to compare output data
                out_adc = None
                for abit, input_s in enumerate(input_chunk):
                    for wbit, weight_s in enumerate(weight_chunk):
                        out_tmp = self._split_forward(input_s, weight_s, ignore_bias=True, cat_output=True, infer_only=True)
                        # out_tmp = F.linear(input_s[:,nIF_cnt:nIF_cnt+self.split_nIF[idx]], weight_s, bias=None)
                        
                        if self.is_noise:
                            if (self.mapping_mode=='2T2R') or (self.mapping_mode=='ref_a'):
                                if wbit == 0:
                                    temp = out_tmp
                                    continue
                                else:
                                    out_tmp = (temp - out_tmp) / delta_G
                                    # temp = [temp - out_tmp for (temp, out_tmp) in zip(temp, out_tmp)]
                                    # out_tmp = list(map(lambda x: x/delta_G, temp))
                            else:
                                out_tmp /= delta_G
                                # out_tmp = list(map(lambda x: x/delta_G, out_tmp))
                        out_tmp = torch.chunk(out_tmp, self.split_groups, dim=1)
                        out_tmp = list(map(lambda x: x.contiguous(), out_tmp))
                    
                        a_mag, w_mag, cell_scale = self._output_magnitude(abit, wbit, wsplit_num)
                        out_adc = psum_quant_merge(out_adc, out_tmp,
                                                    pbits=self.pbits, step=self.pstep, 
                                                    half_num_levels=self.phalf_num_levels, 
                                                    pbound=self.pbound, center=self.center, weight=a_mag,
                                                    groups=self.split_groups, pzero=self.pzero)

                        # weight output summation
                        if self.mapping_mode == 'two_com':
                            if wsplit_num == wbit+1:
                                out_wsum -= out_adc * w_mag / cell_scale
                            else:
                                out_wsum = out_adc if wbit == 0 else out_wsum + out_adc
                        elif (self.mapping_mode == 'ref_d') or (self.mapping_mode == 'PN'):
                            out_wsum = out_adc if wbit == 0 else out_wsum - out_adc
                        else:
                            # out_wsum = out_adc if wbit == 0 else out_wsum + out_adc
                            out_wsum = out_adc # cell bit is not split (not support) only cbits > wbits
                        out_adc = None
                    output = out_wsum if abit == 0 else output+out_wsum

                # restore output's scale
                output = output * psum_scale
        else:
            abit_serial = Bitserial.abit_serial()
            if not abit_serial:
                # in-mem computation mimic (split linear & psum quant/merge)
                self.pbits = 32
                output = self._split_forward(input, qweight, ignore_bias=True, merge_group=True)

                if self.training:
                    with torch.no_grad():

                        sweight, wsplit_num = self._weight_bitserial(qweight, w_scale, cbits=self.cbits)
                        weight_chunk = torch.chunk(sweight, wsplit_num, dim=1)

                        if self.is_noise:
                            self.noise_cell.update_setting(self.noise_param, self.ratio)
                            weight_chunk = self._cell_noise_inject(weight_chunk)
                            delta_G = self.noise_cell.get_deltaG()

                        for wbit, weight_s in enumerate(weight_chunk):
                            out_tmp = self._split_forward(input, weight_s, ignore_bias=True, infer_only=True, merge_group=True)
                            _, w_mag, cell_scale = self._output_magnitude(abit=0, wbit=wbit, split_num=wsplit_num)                 
                            if self.is_noise:
                                if (self.mapping_mode=='2T2R') or (self.mapping_mode=='ref_a'):
                                    if wbit == 0:
                                        temp = out_tmp
                                        continue
                                    else:
                                        out_tmp = (temp - out_tmp) / delta_G
                                else:
                                    out_tmp /= delta_G

                            # weight output summation
                            if self.mapping_mode == 'two_com':
                                if wsplit_num == wbit+1:
                                    out_wsum -= out_tmp * w_mag / cell_scale
                                else:
                                    out_wsum = out_tmp if wbit == 0 else out_wsum + out_tmp
                            elif (self.mapping_mode == 'ref_d') or (self.mapping_mode == 'PN'):
                                out_wsum = out_tmp if wbit == 0 else out_wsum - out_tmp
                            else:
                                # out_wsum = out_adc if wbit == 0 else out_wsum + out_adc
                                out_wsum = out_tmp

                        output = out_wsum * w_scale
            else:
                assert False, "we do not support act serial only model"

        # add bias
        if self.bias is not None:
            output += self.bias
        
        # output_real = F.linear(input, qweight, bias=None)
        # import pdb; pdb.set_trace()

        return output

    def forward(self, input):
        if self.bitserial_log:
            return self._bitserial_log_forward(input)
        else:
            if not self.wbit_serial and not self.is_noise and self.wbits==32:
                return F.linear(input, self.weight, bias=self.bias)
            else:
                return self._bitserial_comp_forward(input)

    def extra_repr(self):
        """Provides layer information, including wbits, when print(model) is called."""
        s =  'in_features={}, out_features={}, bias={}, wbits={}, wbit_serial={}, split_groups={}, '\
            'mapping_mode={}, cbits={}, psum_mode={}, pbits={}, pbound={}, '\
            'noise={}, noise_type={}, noise_param={}, cell_ratio={}, bitserial_log={}, layer_idx={}'\
            .format(self.in_features, self.out_features, self.bias is not None, self.wbits, self.wbit_serial,
            self.split_groups, self.mapping_mode, self.cbits, self.psum_mode, self.pbits, self.pbound, 
            self.is_noise, self.noise_type, self.noise_param, self.ratio, self.bitserial_log, self.layer_idx)
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

def set_BitSerial_log(model, pbits, pclipmode, pclip=None, psigma=None, checkpoint=None, pquant_idx=None, pbound=None, center=None, log_file=False,
                        weight_dist=False, graph_path=None):
    print("start setting Bitserial layers log bitplane info")
    counter = 0
    for m in model.modules():
        if type(m).__name__ in ['PsumQConv' , 'PsumQLinear']:
            m.layer_idx = counter
            if (pquant_idx is None) or (counter == pquant_idx):
                m.bitserial_log = log_file
                m.checkpoint = checkpoint
                m.pclipmode = pclipmode
                m.setting_pquant_func(pbits, center, pbound)
                m.pclip = pclip
                m.psigma = psigma
                # weight_dist
                m.weight_dist=weight_dist
                m.graph_path=graph_path
                print("finish setting {}, idx: {}".format(type(m).__name__, counter))
            else:
                print(f"pass {m} with counter {counter}")
            counter += 1

def unset_BitSerial_log(model):
    print("start unsetting Bitserial layers log bitplane info")
    counter = 0
    for m in model.modules():
        if type(m).__name__ in ['PsumQConv' , 'PsumQLinear']:
            m.bitserial_log = False
            print("finish log unsetting {}, idx: {}".format(type(m).__name__, counter))
            counter += 1

def set_bitserial_layer(model, pquant_idx, wbit_serial=None, pbits=32, center=[]):
    ## set block for bit serial computation
    print("start setting conv/fc bitserial layer")
    counter = 0
    for m in model.modules():
        if type(m).__name__ in ['PsumQConv' , 'PsumQLinear']:
            if counter == pquant_idx:
                m.reset_layer(wbit_serial=wbit_serial, pbits=pbits, center=center)
            counter += 1
    print("finish setting conv/fc bitserial layer ")

def set_Qact_bitserial(model, pquant_idx, abit_serial=True):
    ## set quantact bitserial
    print("start setting quantact bitserial")
    counter = 0
    for m in model.modules():
        if type(m).__name__ is 'Q_act':
            if counter == pquant_idx:
                m.bitserial = abit_serial
            counter += 1
    print("finish setting quantact bitserial ")

def set_Noise_injection(model, noise_param=0.1, ratio=1000):
    ## set noise cell injection
    print("start setting noise injection")
    counter = 0
    for m in model.modules():
        if type(m).__name__ in ['PsumQConv' , 'PsumQLinear']:
            m.noise_param = noise_param
            m.ratio = ratio
        counter += 1
    print("finish setting noise injection")

def count_ArrayMaxV(wbits, cbits, mapping_mode, arraySize):
    if mapping_mode == '2T2R':
        if cbits >= (wbits-1):
            aMaxV = (2**(wbits-1)-1) * arraySize
        else:
            assert False, 'This file does not support this case cbits_{} < wbits_{}'.format(cbits, wbits)
    elif mapping_mode == 'two_com':
        if cbits >= (wbits-1):
            aMaxV = (2**(wbits-1)-1) * arraySize
        else:
            assert False, 'This file does not support this case cbits_{} < wbits_{}'.format(cbits, wbits)
    elif mapping_mode == 'ref_d':
        if cbits >= wbits:
            aMaxV = (2**(wbits)-1) * arraySize
        else:
            assert False, 'This file does not support this case cbits_{} < wbits_{}'.format(cbits, wbits)
    else:
        assert False, 'This file does not support the mapping_mode {}'.format(mapping_mode)
    
    return aMaxV