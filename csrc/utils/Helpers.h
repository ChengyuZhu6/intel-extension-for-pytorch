#pragma once

#include <ATen/ATen.h>
#include <CL/sycl.hpp>
#include <c10/core/Device.h>
#include <c10/macros/Macros.h>
#include <utils/Macros.h>

namespace at {
namespace AtenIpexTypeXPU {

sycl::event dpcpp_q_barrier(sycl::queue& q);
sycl::event dpcpp_q_barrier(sycl::queue& q, std::vector<sycl::event>& events);

} // namespace AtenIpexTypeXPU
} // namespace at

namespace xpu {
namespace dpcpp {

/*
 * The namespace at::AtenIpexTypeXPU only serves as operator/kernel
 * implementation. We export operators here under xpu::dpcpp namespace for
 * frontend usage.
 */
EXPORT_TO_XPU_ALIAS(dpcpp_q_barrier, queue_barrier);

} // namespace dpcpp
} // namespace xpu
