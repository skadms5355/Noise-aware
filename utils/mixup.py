from __future__ import print_function, absolute_import
import torch
import numpy as np

__all__ = ['mixup_data', 'mixup_criterion']

"""
    Reference: https://github.com/facebookresearch/mixup-cifar10
    By Hongyi Zhang, Moustapha Cisse, Yann Dauphin, David Lopez-Paz.
    Facebook AI Research

    CC-BY-NC-licensed


    Reference code is Mixup code for cifar10. We can also use this for cifar100.
"""

# functions about mix-up
def mixup_data(x, y, alpha=1.0):
    '''Returns mixed inputs, pairs of targets, and lambda'''
    if alpha > 0:
        lam = np.random.beta(alpha, alpha)
    else:
        lam = 1

    batch_size = x.size()[0]
    index = torch.randperm(batch_size).to("cuda")

    mixed_x = lam * x + (1 - lam) * x[index, :]
    y_a, y_b = y, y[index]
    return mixed_x, y_a, y_b, lam

def mixup_criterion(criterion, pred, y_a, y_b, lam):
    return lam * criterion(pred, y_a) + (1 - lam) * criterion(pred, y_b)
