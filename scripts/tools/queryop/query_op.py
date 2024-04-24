"""
Please set up your conda environment for each backend.

if key == 'CPU' or 'CUDA:
"Please set up your PyTorch environment: install pytorch-2.1 and build with CUDA"
"pytorch: https://github.com/pytorch/pytorch/tree/release/2.1"

if key == 'XPU':
"Please set up your XPU environment: install pytorch-2.1 and Intel® Extension for PyTorch* GPU"
"Intel® Extension for PyTorch* GPU: https://github.com/intel/intel-extension-for-pytorch/tree/xpu-main"

usage example:
if key == CPU or CUDA:
    python query_op.py --key CPU --output filename
    python query_op.py --key CUDA --output filename
elif key == XPU:
    python query_op.py --cpu_queried_file "/path/cpu" --cuda_queried_file "/path/cuda" --output filename
"""

import argparse
import sys
import os
import subprocess
import torch
import pandas as pd
import numpy as np

import yaml
import pathlib
from collections import defaultdict
from functools import reduce

# add scripts folder path
scripts_root = pathlib.Path(__file__).parent.parent.parent.parent.absolute()
sys.path.append(scripts_root.as_posix())

from torchgen.model import FunctionSchema
# Safely load fast C Yaml loader/dumper if they are available
try:
    from yaml import CSafeLoader as YamlLoader
except ImportError:
    from yaml import SafeLoader as YamlLoader  # type: ignore[misc]

parser = argparse.ArgumentParser(description='Query operator registrations')
parser.add_argument('--key', default='XPU', type=str, help='Dispatch keys')
# torch contains aten ops and other ops such as caffe2, quantized, sparse, static_runtime..., and we are querying the aten ops, and so use aten as default values.
parser.add_argument('--type', default='aten', type=str, help='Operator types')
parser.add_argument('--output', help='Output Files')
parser.add_argument('--cpu_queried_file', type=str, help='Path of the cpu file')
parser.add_argument('--cuda_queried_file', type=str, help='Path of the cuda file')

def get_opvector(args, backend=''):
    # This functions uses pytorch API to print all operators that have kernels registered to a particular DispatchKey
    # Ops are obtained from operatorLookupTable
    # See more details in https://github.com/pytorch/pytorch/issues/63575

    if args.key == 'CUDA' or args.key == 'CPU':
        cmd = "python -c 'import torch; torch._C._dispatch_print_registrations_for_dispatch_key(\"" + backend + "\")' > /tmp/log.txt"
    else:
        cmd = "python -c 'import torch; import intel_extension_for_pytorch; torch._C._dispatch_print_registrations_for_dispatch_key(\"" + backend + "\")' > /tmp/log.txt"
    subprocess.check_call(cmd, shell=True)
    op_vector = []
    with open("/tmp/log.txt", 'r') as f:
        for line in f.readlines():
            op_vector.append(line.strip())
    return op_vector


def query_ops_key(op_all, key_types, args):  
    len_op = len(op_all)
    len_keytpye = len(key_types)

    op_registration = [[''] * len_keytpye for _ in range(len_op + 2)]
    for k in range(len_keytpye):
        dispatch_key = key_types[k] + args.key
        op_key_alltype = get_opvector(args, dispatch_key)
        op_key = list(filter(lambda x : args.type in x , op_key_alltype))

        # get op number and percent for each key type
        op_registration[0][k] = len(op_key)    # number
        op_registration[1][k] = '{:.3f}'.format(len(op_key) / len_op)    # percent

        # determine whether this op has been registrated
        for i in range(len_op):
            if op_all[i] in op_key:
                op_registration[i + 2][k] = 'Y'
    return op_registration             


def filter_op(path):
    with open(path, 'r') as f:
        func_declarations = yaml.load(f, Loader=YamlLoader)

    op_structured_delegate = defaultdict(str)
    op_register_name = defaultdict(str)
    op_register_simple = defaultdict(lambda: 'No')
    op_cuda_specific = []

    for func_declaration in func_declarations:
        func = func_declaration.get('func')
        func_schema = FunctionSchema.parse(func)
        opname_str = 'aten::' + func_schema.name.__str__()
        if 'cuda' in opname_str or 'cudnn' in opname_str:
            op_cuda_specific.append(opname_str)
        # structure delegate
        structured_delegate_s = func_declaration.pop('structured_delegate', None)
        if structured_delegate_s is not None:
            op_structured_delegate[opname_str] = structured_delegate_s

        # determine op register or not
        raw_dispatch = func_declaration.pop('dispatch', None)
        if raw_dispatch is not None and raw_dispatch != {}:
            key_dispatch = list(raw_dispatch.keys())

            # include CompositeImplicitAutograd ---- Not register
            if 'CompositeImplicitAutograd' in key_dispatch:
                op_register_name[opname_str] = 'No'

            # include CompositeExplicitAutograd ---- Optional
            elif 'CompositeExplicitAutograd' in key_dispatch:
                op_register_name[opname_str] = 'Optional'

            # other dispatchkey ---- Register
            else:
                op_register_name[opname_str] = key_dispatch
                if any('CUDA' in item.split(", ") for item in key_dispatch):
                    op_register_simple[opname_str] = 'Yes'
        else:    # no dispatch
            op_register_name[opname_str] = 'No'
    return op_structured_delegate, op_register_name, op_register_simple, op_cuda_specific

def main():            
    args = parser.parse_args()
    native_yaml_path = os.path.join(scripts_root, 'scripts/tools/torchgen/packaged/ATen/native/native_functions.yaml')

    # get all operators
    op_alltype = get_opvector(args)
    # filter the aten operators
    op_all = list(filter(lambda x : args.type in x , op_alltype))
    op_all.sort()

    # get operators for a specific backend
    key_types = ['', 'Quantized', 'Sparse']
    op_registration_backend = query_ops_key(op_all, key_types, args)

    # set a dataframe and output to csv
    col_name = [key_type + args.key for key_type in key_types] 
    row_name = ['Number', 'Percent'] + op_all
    output_data = pd.DataFrame(columns=col_name, index=row_name, data=op_registration_backend)

    op_label = "Total_Ops"
    if args.key == 'CPU' or args.key == 'CUDA':
        if not args.cpu_queried_file and not args.cuda_queried_file:
            # output directly
            output_data.to_csv(args.output, index_label=op_label)
            return
        else:
            print('Merge is not supported!')
            sys.exit(1)

    tmp_file = "xpu_ops.csv"
    output_data.to_csv(tmp_file, index_label=op_label)

    # merge all backend files into a single one
    print("----------Merge files into a single one----------")
    if not args.cpu_queried_file or not args.cuda_queried_file:
        print("Please input CPU file and CUDA file!")
        sys.exit(1)

    # load data
    cpu_data = pd.read_csv(args.cpu_queried_file, na_filter=False)
    cuda_data = pd.read_csv(args.cuda_queried_file, na_filter=False)
    xpu_data = pd.read_csv(tmp_file, na_filter=False)

    # merge file
    # Step1: get the reference (get the union of CPU and CUDA first)
    merged_cpu_cuda = pd.merge(cpu_data, cuda_data, how='outer', on=op_label)
    
    # Step2: get the union for CPU, CUDA, XPU
    merged_file = pd.merge(merged_cpu_cuda, xpu_data, how='outer', on=op_label)
    
    
    list_cpu_cuda = list(merged_cpu_cuda[op_label])
    list_xpu = list(xpu_data[op_label])
    # filter ops in cpu_cuda but not in xpu
    ret_incpucuda_outxpu = list(set(list_cpu_cuda).difference(set(list_xpu)))
    # filter ops in xpu but not in cpu_cuda
    ret_inxpu_outcpucuda = list(set(list_xpu).difference(set(list_cpu_cuda)))

    # reorder the column names (put CPU, CUDA, XPU together)
    cols_num = merged_file.shape[1]
    cols_idx = [0] + list(np.arange(1, cols_num).reshape(3, -1).flatten('F'))
    cols_reorder = merged_file.columns[cols_idx]
    merged_file = merged_file[cols_reorder]

    #  filter operators
    op_structured_delegate, op_register_name, op_register_simple, op_cuda_specific = filter_op(native_yaml_path)

    # op classification
    # op_all_merge is the common ops across all backends
    op_all_merge = list(merged_file.iloc[2:,0])
    op_size = len(op_all_merge)
    op_category = [[''] * 4 for _ in range(op_size + 2)]
    op_category[0][0] = len(op_structured_delegate)
    op_category[1][0] = '{:.3f}'.format(len(op_structured_delegate) / op_size)

    for i in range(op_size):
        # structure delegate
        if op_all_merge[i] in op_structured_delegate.keys():
            op_category[i + 2][0] = op_structured_delegate[op_all_merge[i]]
        # dispatch information
        if op_all_merge[i] in op_register_name.keys():
            op_category[i + 2][1] = op_register_name[op_all_merge[i]]
            op_category[i + 2][2] = op_register_simple[op_all_merge[i]]


    op_category_df = pd.DataFrame(columns=['structured_delegate', 'register', 'register or not', 'difference'], data=op_category)

    mergefile_concat = pd.concat([merged_file, op_category_df], axis=1)
    
    # specific conditions
    # mark the cuda specific ops as 'S'
    for item_cuda in op_cuda_specific:
        row_idx = mergefile_concat[mergefile_concat[op_label] == item_cuda].index.values
        mergefile_concat.loc[row_idx, 'CUDA'] = 'S'
    # mark the ops in cpu_cuda but not in xpu
    for item_cpu_cuda in ret_incpucuda_outxpu:
        row_idx = mergefile_concat[mergefile_concat[op_label] == item_cpu_cuda].index.values
        mergefile_concat.loc[row_idx, 'difference'] = '2.1'
    # mark the ops in xpu but not in xpu and cpu_cuda
    for item_xpu in ret_inxpu_outcpucuda:
        row_idx = mergefile_concat[mergefile_concat[op_label] == item_xpu].index.values
        mergefile_concat.loc[row_idx, 'difference'] = '2.0' 
       
    mergefile_concat.to_csv(args.output, index=None)
    print("----------Merge finished!----------")


if __name__ == '__main__':
    main()
