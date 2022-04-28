import torch
import torch.nn.functional
from torch import nn as nn
from torch.testing._internal.common_utils import TestCase
import intel_extension_for_pytorch
import pytest
from torch.optim import Optimizer
import math

device = 'xpu'
num_iter = 5
checking_atol = 1e-3
checking_rtol = 3e-3

lr = 0.01
beta1 = 0.9
beta2 = 0.999
adam_epsilon = 1e-6
weight_decay = 0.01
dtype = torch.bfloat16

# model cpu
class ModelCPU(nn.Module):
    def __init__(self):
        super(ModelCPU, self).__init__()
        self.m = nn.Sequential(
            nn.Conv2d(512, 2048, kernel_size=(1, 1), stride=(1, 1), bias=False),
            nn.BatchNorm2d(2048, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),
            nn.AvgPool2d(kernel_size=7, stride=1, padding=0),
        )
        self.fc = nn.Linear(in_features=2048, out_features=1000, bias=True)

    def forward(self, x):
        x = self.m(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)
        return x

# model xpu
class ModelXPU(nn.Module):
    def __init__(self):
        super(ModelXPU, self).__init__()
        self.m = nn.Sequential(
            nn.Conv2d(512, 2048, kernel_size=(1, 1), stride=(1, 1), bias=False),
            nn.BatchNorm2d(2048, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),
            nn.AvgPool2d(kernel_size=7, stride=1, padding=0),
        )
        self.fc = nn.Linear(in_features=2048, out_features=1000, bias=True)

    def forward(self, x):
        x = self.m(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)
        return x


class CPUReferenceAdamMasterWeight(Optimizer):
    def __init__(self, params, lr=1e-3, betas=(0.9, 0.999), eps=1e-8,
                 weight_decay=1e-2, amsgrad=False, transformer=False, correct_bias=True):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))
        if not 0.0 <= weight_decay:
            raise ValueError("Invalid weight_decay value: {}".format(weight_decay))
        if transformer and amsgrad:
            raise ValueError("Invalid combination for attribute transformer and amsgrad.")
        if not transformer and not correct_bias:
            raise ValueError("Invalid combination for attribute transformer and correct bias.")
        defaults = dict(lr=lr, betas=betas, eps=eps,
                        weight_decay=weight_decay, amsgrad=amsgrad, transformer=transformer,
                        correct_bias=correct_bias)
        super(CPUReferenceAdamMasterWeight, self).__init__(params, defaults)

    def __setstate__(self, state):
        super(CPUReferenceAdamMasterWeight, self).__setstate__(state)
        for group in self.param_groups:
            group.setdefault('amsgrad', False)

    @torch.no_grad()
    def step(self, closure=None):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue

                # Perform stepweight decay
                if not group['transformer']:
                    p.mul_(1 - group['lr'] * group['weight_decay'])

                # Perform optimization step
                grad = p.grad.to(p.dtype)

                if grad.is_sparse:
                    raise RuntimeError('CPUReferenceAdamMasterWeight does not support sparse gradients')

                state = self.state[p]

                # State initialization
                if len(state) == 0:
                    state['step'] = 0
                    state['exp_avg'] = torch.zeros_like(p, memory_format=torch.preserve_format)
                    state['exp_avg_sq'] = torch.zeros_like(p, memory_format=torch.preserve_format)
                    state['max_exp_avg_sq'] = torch.Tensor().xpu()
                    if group['amsgrad']:
                        state['max_exp_avg_sq'] = torch.zeros_like(p, memory_format=torch.preserve_format)

                # get value
                exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']
                if group['amsgrad']:
                    max_exp_avg_sq = state['max_exp_avg_sq']
                beta1, beta2 = group['betas']

                state['step'] += 1

                if not group['transformer'] or group['correct_bias']:
                    bias_correction1 = 1 - beta1 ** state['step']
                    bias_correction2 = 1 - beta2 ** state['step']

                # Decay the first and second moment running average coefficient
                exp_avg.mul_(beta1).add_(grad, alpha=1 - beta1)
                exp_avg_sq.mul_(beta2).addcmul_(grad, grad, value=1 - beta2)

                if group['amsgrad']:
                    torch.maximum(max_exp_avg_sq, exp_avg_sq, out=max_exp_avg_sq)
                    denom = (max_exp_avg_sq.sqrt() / math.sqrt(bias_correction2)).add_(group['eps'])
                elif not group['transformer']:
                    denom = (exp_avg_sq.sqrt() / math.sqrt(bias_correction2)).add_(group['eps'])
                else:
                    denom = exp_avg_sq.sqrt().add_(group['eps'])

                step_size = group['lr']
                if not group['transformer']:
                    step_size = group['lr'] / bias_correction1
                elif group['correct_bias']:
                    step_size = step_size * math.sqrt(bias_correction2) / bias_correction1

                p.addcdiv_(exp_avg, denom, value=-step_size)

                if group['transformer'] and group['weight_decay'] > 0.0:
                    p.data.add_(-group['lr'] * group['weight_decay'], p.data)

        return loss

class TestNNMethod(TestCase):
    @pytest.mark.skipif(torch.xpu.using_layout_opt(), reason="AdamW optimizer does not support onednn block format")
    def create_model(self, transformer, correct_bias, amsgrad):
        # create model
        global ModelCPU
        global ModelXPU
        model_cpu = ModelCPU()
        model_cpu.train()
        model_xpu = ModelXPU()
        model_xpu.train()

        # align the master weight in cpu model and xpu model in float32
        p_cpu_list = list(model_cpu.parameters())
        p_xpu_list = list(model_xpu.parameters())
        for k in range(len(p_cpu_list)):
            p_xpu_list[k].data = p_cpu_list[k].detach().clone().to(device='xpu', dtype=torch.float32).data
            torch.xpu.synchronize()

        # optimizer
        optimizer_cpu = CPUReferenceAdamMasterWeight(model_cpu.parameters(),
                                                     lr=lr,
                                                     betas=(beta1, beta2),
                                                     eps=adam_epsilon,
                                                     weight_decay=weight_decay,
                                                     transformer=transformer,
                                                     correct_bias=correct_bias)
        optimizer_xpu = torch.xpu.optim.AdamWMasterWeight(model_xpu.parameters(),
                                                          lr=lr,
                                                          betas=(beta1, beta2),
                                                          eps=adam_epsilon,
                                                          weight_decay=weight_decay,
                                                          transformer=transformer,
                                                          correct_bias=correct_bias)

        # model xpu bf16
        model_xpu = model_xpu.to(device=device, dtype=torch.bfloat16)
        return model_cpu, model_xpu, optimizer_cpu, optimizer_xpu

    def training_step(self, model_cpu, model_xpu, optimizer_cpu, optimizer_xpu, mem_format):
        input = torch.randn(128, 512, 7, 7)
        target = torch.empty(128, dtype=torch.long).random_(1000)

        input_xpu = input.clone().to(device=device, dtype=dtype).requires_grad_()
        input_xpu = input_xpu.contiguous(memory_format=mem_format)
        target_xpu = target.to(device)
        input_cpu = input.clone().float().cpu().requires_grad_()
        input_cpu = input_cpu.contiguous(memory_format=mem_format)
        target_cpu = target.detach().clone().cpu()

        # forward
        output_cpu = model_cpu(input_cpu)
        output_xpu = model_xpu(input_xpu)
        torch.xpu.synchronize()
        # align output
        output_cpu.data = output_xpu.detach().clone().cpu().float().data
        torch.xpu.synchronize()

        # loss
        criterion = nn.CrossEntropyLoss()
        loss_cpu = criterion(output_cpu, target_cpu)
        loss_xpu = criterion(output_xpu, target_xpu)
        torch.xpu.synchronize()

        # align loss
        loss_xpu.data = loss_cpu.clone().to(device='xpu').data
        torch.xpu.synchronize()

        # optimizer
        optimizer_cpu.zero_grad()
        optimizer_xpu.zero_grad()

        # backward
        loss_cpu.backward()
        loss_xpu.backward()
        torch.xpu.synchronize()

        # align grad
        p_cpu_list = list(model_cpu.parameters())
        p_xpu_list = list(model_xpu.parameters())
        for k in range(len(p_cpu_list)):
            p_xpu_list[k].grad.data = p_cpu_list[k].grad.detach().clone().to(device='xpu', dtype=torch.bfloat16).data
            torch.xpu.synchronize()

        # update
        optimizer_cpu.step()
        optimizer_xpu.step()
        torch.xpu.synchronize()

    def check_result(self, model_xpu, model_cpu):
        # checking updated weight
        for layer1 in model_xpu.modules():
            for layer2 in model_cpu.modules():
                if (isinstance(layer1, nn.BatchNorm2d) and isinstance(layer2, nn.BatchNorm2d)):
                    bn_xpu_weight = layer1.weight.clone().cpu().float()
                    bn_xpu_bias = layer1.bias.clone().cpu().float()
                    bn_cpu_weight = layer2.weight.clone().cpu().float()
                    bn_cpu_bias = layer2.bias.clone().cpu().float()

                    # checking
                    self.assertEqual(bn_cpu_weight, bn_xpu_weight.cpu().float(), atol=checking_atol, rtol=checking_rtol)
                    self.assertEqual(bn_cpu_bias, bn_xpu_bias.cpu().float(), atol=checking_atol, rtol=checking_rtol)

                if (isinstance(layer1, nn.Conv2d) and isinstance(layer2, nn.Conv2d)):
                    conv_xpu_weight = layer1.weight.clone().cpu().float()
                    conv_cpu_weight = layer2.weight.clone().cpu().float()

                    # checking
                    self.assertEqual(conv_cpu_weight, conv_xpu_weight.cpu().float(), atol=checking_atol, rtol=checking_rtol)

                if (isinstance(layer1, nn.Linear) and isinstance(layer2, nn.Linear)):
                    fc_xpu_weight = layer1.weight.clone().cpu().float()
                    fc_xpu_bias = layer1.bias.clone().cpu().float()
                    fc_cpu_weight = layer2.weight.clone().cpu().float()
                    fc_cpu_bias = layer2.bias.clone().cpu().float()

                    # checking
                    self.assertEqual(fc_cpu_weight, fc_xpu_weight.cpu().float(), atol=checking_atol, rtol=checking_rtol)
                    self.assertEqual(fc_cpu_bias, fc_xpu_bias.cpu().float(), atol=checking_atol, rtol=checking_rtol)

    def test_FusedAdamWMasterWeight_transformer(self, dtype=torch.bfloat16):
        transformer = True
        correct_bias = True
        amsgrad = False
        # ### test contiguous_format ### #
        mem_format = torch.contiguous_format
        model_cpu, model_xpu, optimizer_cpu, optimizer_xpu = self.create_model(transformer, correct_bias, amsgrad)
        for i in range(num_iter):
            print('\n\niter: ', i)
            self.training_step(model_cpu, model_xpu, optimizer_cpu, optimizer_xpu, mem_format)
            self.check_result(model_xpu, model_cpu)

        # ### test channels_last format ### #
        mem_format = torch.channels_last
        model_cpu, model_xpu, optimizer_cpu, optimizer_xpu = self.create_model(transformer, correct_bias, amsgrad)
        model_cpu = model_cpu.to(memory_format=mem_format)
        model_xpu = model_xpu.to(memory_format=mem_format)
        for i in range(num_iter):
            print('\n\niter: ', i)
            self.training_step(model_cpu, model_xpu, optimizer_cpu, optimizer_xpu, mem_format)
            self.check_result(model_xpu, model_cpu)

    def test_FusedAdamWMasterWeight_transformer_no_correct_bias(self, dtype=torch.bfloat16):
        transformer = True
        correct_bias = False
        amsgrad = False
        # ### test contiguous_format ### #
        mem_format = torch.contiguous_format
        model_cpu, model_xpu, optimizer_cpu, optimizer_xpu = self.create_model(transformer, correct_bias, amsgrad)
        for i in range(num_iter):
            print('\n\niter: ', i)
            self.training_step(model_cpu, model_xpu, optimizer_cpu, optimizer_xpu, mem_format)
            self.check_result(model_cpu, model_xpu)

        # ### test channels_last format ### #
        mem_format = torch.channels_last
        model_cpu, model_xpu, optimizer_cpu, optimizer_xpu = self.create_model(transformer, correct_bias, amsgrad)
        model_cpu = model_cpu.to(memory_format=mem_format)
        model_xpu = model_xpu.to(memory_format=mem_format)
        for i in range(num_iter):
            print('\n\niter: ', i)
            self.training_step(model_cpu, model_xpu, optimizer_cpu, optimizer_xpu, mem_format)
            self.check_result(model_xpu, model_cpu)

    def test_FusedAdamWMasterWeight_official(self, dtype=torch.bfloat16):
        transformer = False
        correct_bias = True
        amsgrad = False
        # ### test contiguous_format ### #
        model_cpu, model_xpu, optimizer_cpu, optimizer_xpu = self.create_model(transformer, correct_bias, amsgrad)
        mem_format = torch.contiguous_format
        for i in range(num_iter):
            print('\n\niter: ', i)
            self.training_step(model_cpu, model_xpu, optimizer_cpu, optimizer_xpu, mem_format)
            self.check_result(model_cpu, model_xpu)

        # ### test channels_last format ### #
        mem_format = torch.channels_last
        model_cpu, model_xpu, optimizer_cpu, optimizer_xpu = self.create_model(transformer, correct_bias, amsgrad)
        model_cpu = model_cpu.to(memory_format=mem_format)
        model_xpu = model_xpu.to(memory_format=mem_format)
        for i in range(num_iter):
            print('\n\niter: ', i)
            self.training_step(model_cpu, model_xpu, optimizer_cpu, optimizer_xpu, mem_format)
            self.check_result(model_xpu, model_cpu)

    def test_FusedAdamWMasterWeight_official_amsgrad(self, dtype=torch.bfloat16):
        transformer = False
        correct_bias = True
        amsgrad = True
        # ### test contiguous_format ### #
        model_cpu, model_xpu, optimizer_cpu, optimizer_xpu = self.create_model(transformer, correct_bias, amsgrad)
        mem_format = torch.contiguous_format
        for i in range(num_iter):
            print('\n\niter: ', i)
            self.training_step(model_cpu, model_xpu, optimizer_cpu, optimizer_xpu, mem_format)
            self.check_result(model_cpu, model_xpu)

        # ### test channels_last format ### #
        mem_format = torch.channels_last
        model_cpu, model_xpu, optimizer_cpu, optimizer_xpu = self.create_model(transformer, correct_bias, amsgrad)
        model_cpu = model_cpu.to(memory_format=mem_format)
        model_xpu = model_xpu.to(memory_format=mem_format)
        for i in range(num_iter):
            print('\n\niter: ', i)
            self.training_step(model_cpu, model_xpu, optimizer_cpu, optimizer_xpu, mem_format)
            self.check_result(model_xpu, model_cpu)
