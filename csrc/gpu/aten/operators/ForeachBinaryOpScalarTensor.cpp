/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

#include <ATen/Dispatch.h>
#include <ATen/native/Fill.h>
#include <ATen/native/ForeachUtils.h>
#include <ATen/native/TensorIterator.h>
#include <aten/core/detail/IndexUtils.h>

#include <runtime/Utils.h>
#include "ATen/OpMathType.h"
#include "comm/ATDispatch.h"
#include "comm/ApplyUtils.h"
#include "comm/RegistrationDeclarations.h"

#include <ATen/native/BinaryOps.h>
#include "ForeachFunctors.h"
#include "Loops.h"
#include "MultiTensorApply.h"
#include "comm/Numerics.h"

namespace at {
namespace AtenIpexTypeXPU {

template <typename T, template <class> class Op>
std::vector<Tensor> foreach_binary_op(
    at::TensorList tensors,
    const at::Tensor& scalar) {
  TORCH_CHECK(
      scalar.dim() == 0 && scalar.numel() == 1,
      "scalar tensor expected to be 0 dim but it has ",
      scalar.dim(),
      " dimensions and ",
      scalar.numel(),
      " elements.");
  TORCH_CHECK(
      tensors[0].device() == scalar.device(),
      "scalar tensor expected to be on ",
      tensors[0].device(),
      " but is on ",
      scalar.device());
  std::vector<std::vector<at::Tensor>> tensor_lists;
  std::vector<at::Tensor> vec_res;
  vec_res.reserve(tensors.size());
  for (const auto& t : tensors) {
    vec_res.emplace_back(at::native::empty_like(t));
  }

  tensor_lists.emplace_back(tensors.vec());
  tensor_lists.emplace_back(std::move(vec_res));

  using opmath_t = at::opmath_type<T>;
  multi_tensor_apply<2>(
      tensor_lists,
      BinaryOpScalarTensorFunctor<
          T,
          /* depth */ 2,
          /* r_args_depth */ 1,
          /* res_arg_index */ 1>(),
      Op<opmath_t>(),
      scalar.data_ptr<T>());
  return tensor_lists[1];
}

template <typename T, template <class> class Op>
void foreach_binary_op_(at::TensorList tensors, const at::Tensor& scalar) {
  TORCH_CHECK(
      scalar.dim() == 0 && scalar.numel() == 1,
      "scalar tensor expected to be 0 dim but has ",
      scalar.dim(),
      " dimensions and ",
      scalar.numel(),
      " elements.");
  TORCH_CHECK(
      tensors[0].device() == scalar.device(),
      "scalar tensor is expected to be on ",
      tensors[0].device(),
      " but is on ",
      scalar.device());
  std::vector<std::vector<at::Tensor>> tensor_lists;
  tensor_lists.emplace_back(tensors.vec());

  using opmath_t = at::opmath_type<T>;
  multi_tensor_apply<1>(
      tensor_lists,
      BinaryOpScalarTensorFunctor<
          T,
          /* depth */ 1,
          /* r_args_depth */ 1,
          /* res_arg_index */ 0>(),
      Op<opmath_t>(),
      scalar.data_ptr<T>());
  increment_version(tensors);
}

// TODO(crcrpar): Nest dispatch by looking up `scalar.scalar_type` for better
// coverage?
#define FOREACH_BINARY_OP_SCALAR_TENSOR(FUNCTION, NAME, OP, DIVISION_OP)      \
  void _foreach_##NAME##_(at::TensorList tensors, const at::Tensor& scalar) { \
    at::native::check_foreach_api_restrictions(tensors);                      \
    if (!(at::native::can_use_fast_route(                                     \
              at::ArrayRef<TensorList>{tensors}, {}, DIVISION_OP) &&          \
          tensors[0].scalar_type() == scalar.scalar_type())) {                \
      return at::native::foreach_tensor_##NAME##_tensor_kernel_slow_(         \
          tensors, scalar);                                                   \
    }                                                                         \
                                                                              \
    FUNCTION##_<OP>(tensors, scalar);                                         \
  }                                                                           \
                                                                              \
  std::vector<Tensor> _foreach_##NAME(                                        \
      at::TensorList tensors, const at::Tensor& scalar) {                     \
    at::native::check_foreach_api_restrictions(tensors);                      \
    if (!(at::native::can_use_fast_route(                                     \
              at::ArrayRef<TensorList>{tensors}, {}, DIVISION_OP) &&          \
          tensors[0].scalar_type() == scalar.scalar_type())) {                \
      return at::native::foreach_tensor_##NAME##_tensor_kernel_slow(          \
          tensors, scalar);                                                   \
    }                                                                         \
                                                                              \
    return FUNCTION<OP>(tensors, scalar);                                     \
  }

template <template <class> class Op>
std::vector<Tensor> all_types_complex_bool_half_bfloat16(
    at::TensorList tensors,
    const at::Tensor& scalar) {
  return IPEX_DISPATCH_ALL_TYPES_AND_COMPLEX_AND3(
      kBool,
      kHalf,
      kBFloat16,
      tensors[0].scalar_type(),
      "foreach_binary_op_scalar_cuda",
      [&]() { return foreach_binary_op<scalar_t, Op>(tensors, scalar); });
}

template <template <class> class Op>
void all_types_complex_bool_half_bfloat16_(
    at::TensorList tensors,
    const at::Tensor& scalar) {
  IPEX_DISPATCH_ALL_TYPES_AND_COMPLEX_AND3(
      kBool,
      kHalf,
      kBFloat16,
      tensors[0].scalar_type(),
      "foreach_binary_op_scalar_cuda_",
      [&]() { foreach_binary_op_<scalar_t, Op>(tensors, scalar); });
}

FOREACH_BINARY_OP_SCALAR_TENSOR(
    all_types_complex_bool_half_bfloat16,
    mul,
    std::multiplies,
    /* div_op */ false);
} // namespace AtenIpexTypeXPU
} // namespace at
