#===============================================================================
# Copyright 2019 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#===============================================================================

# Manage SYCL-related compiler flags
#===============================================================================

if(DPCPP_cmake_included)
    return()
endif()
set(DPCPP_cmake_included true)

cmake_minimum_required(VERSION 3.4.3)

include(FindPackageHandleStandardArgs)

set(sycl_root_hint)
if(DEFINED DPCPP_ROOT)
    set(sycl_root_hint ${DPCPP_ROOT})
elseif(DEFINED ENV{DPCPP_ROOT})
    set(sycl_root_hint $ENV{DPCPP_ROOT})
endif()

set(sycl_root_hints)
if(sycl_root_hint)
    list(APPEND sycl_root_hints ${sycl_root_hint})
else()
    list(APPEND sycl_root_hints ${SYCL_BUNDLE_ROOT})
    list(APPEND sycl_root_hints $ENV{SYCL_BUNDLE_ROOT})
endif()

# Try to find Intel SYCL version.hpp header
find_file(INTEL_SYCL_VERSION
    NAMES version.hpp
    PATHS
        ${sycl_root_hints}
    PATH_SUFFIXES
        include/CL/sycl
        include/sycl/CL/sycl
        lib/clang/11.0.0/include/CL/sycl
        lib/clang/10.0.0/include/CL/sycl
        lib/clang/9.0.0/include/CL/sycl
        lib/clang/8.0.0/include/CL/sycl
    NO_DEFAULT_PATH)

set(USE_PSTL OFF)
set(USE_DPCPP OFF)
set(USE_COMPUTECPP OFF)
if(INTEL_SYCL_VERSION)
    set(USE_DPCPP ON)
    get_filename_component(SYCL_INCLUDE_DIR "${INTEL_SYCL_VERSION}/../../.." ABSOLUTE)

    find_library(SYCL_LIBRARY
        NAMES "sycl"
        HINTS ${sycl_root_hints}
        PATH_SUFFIXES lib
        NO_DEFAULT_PATH)
    if(NOT SYCL_LIBRARY)
        message(FATAL_ERROR "SYCL library not found")
    endif()

    # Find the OpenCL library from the SYCL distribution
    find_library(OpenCL_LIBRARY
        NAMES "OpenCL"
        HINTS ${sycl_root_hints}
        PATH_SUFFIXES lib
        NO_DEFAULT_PATH)
    if(NOT OpenCL_LIBRARY)
        message(FATAL_ERROR "OpenCL library not found")
    endif()
    set(OpenCL_INCLUDE_DIR ${SYCL_INCLUDE_DIR} CACHE STRING "")

    if(NOT ${SYCL_INCLUDE_DIR} STREQUAL ${OpenCL_INCLUDE_DIR})
        include_directories(${OpenCL_INCLUDE_DIR})
    endif()
    include_directories(${SYCL_INCLUDE_DIR})

    #find LevelZero
    find_path(LevelZero_INCLUDE_DIR
        NAMES level_zero/ze_api.h
        PATH_SUFFIXES include)

    find_library(LevelZero_LIBRARY
        NAMES level_zero
        PATHS
        PATH_SUFFIXES lib/x64 lib lib64)

    if (NOT LevelZero_LIBRARY)
      message(STATUS "LevelZero library not found")
    else()
      set(LevelZero_LIBRARIES ${LevelZero_LIBRARY})
      set(LevelZero_INCLUDE_DIRS ${LevelZero_INCLUDE_DIR})
    endif()

    #TODO: remove TBB
    find_path(TBB_INCLUDE_DIRS
            NAMES tbb
            PATHS ${INTELONEAPIROOT}/tbb/latest $ENV{INTELONEAPIROOT}/tbb/latest
            PATH_SUFFIXES include
            NO_DEFAULT_PATH)

    find_package_handle_standard_args(TBB
            FOUND_VAR TBB_FOUND
            REQUIRED_VARS TBB_INCLUDE_DIRS)
    if(NOT ${TBB_FOUND})
        message(WARNING "TBB not found. No PSTL")
        return()
    endif()

    #add pstl lib
    # Try to find PSTL header from DPC++
    find_path(PSTL_INCLUDE_DIRS
            NAMES dpstd
            PATHS ${sycl_root_hints}
            PATH_SUFFIXES include
            NO_DEFAULT_PATH)

    find_package_handle_standard_args(PSTL
            FOUND_VAR PSTL_FOUND
            REQUIRED_VARS PSTL_INCLUDE_DIRS)
    if(${PSTL_FOUND})
      set(USE_PSTL ON)
      find_library(TBB_LIBRARY
              NAMES tbb
              HINTS ${INTELONEAPIROOT}/tbb/latest $ENV{INTELONEAPIROOT}/tbb/latest
              PATH_SUFFIXES lib/intel64/gcc4.8
              NO_DEFAULT_PATH)
      if(NOT TBB_LIBRARY)
          message(FATAL_ERROR "TBB library not found")
      endif()
      list(APPEND EXTRA_SHARED_LIBS ${TBB_LIBRARY})

      if(NOT ${SYCL_INCLUDE_DIR} STREQUAL ${PSTL_INCLUDE_DIRS})
          include_directories(${PSTL_INCLUDE_DIRS})
      endif()

      if(NOT ${SYCL_INCLUDE_DIR} STREQUAL ${TBB_INCLUDE_DIRS})
          include_directories(${TBB_INCLUDE_DIRS})
      endif()

      message(STATUS "TBB directors " ${TBB_INCLUDE_DIRS})
    else()
      message(WARNING "PSTL not found. No PSTL")
    endif()
    set(TBB_FOUND)
else()
    # ComputeCpp-specific flags
    # 1. Ignore the warning about undefined symbols in SYCL kernels - comes from
    #    SYCL CPU thunks
    # 2. Fix remark [Computecpp:CC0027] about memcpy/memset intrinsics
    set(COMPUTECPP_USER_FLAGS
        -Wno-sycl-undef-func
        -no-serial-memop
        CACHE STRING "")
    set(ComputeCpp_DIR ${sycl_root_hint})
    include(cmake/Modules/FindComputeCpp.cmake)
    if(NOT ComputeCpp_FOUND)
        message(FATAL_ERROR "SYCL not found")
    endif()

    set(USE_COMPUTECPP ON)
    include_directories(SYSTEM ${ComputeCpp_INCLUDE_DIRS})
    list(APPEND EXTRA_SHARED_LIBS ${COMPUTECPP_RUNTIME_LIBRARY})

    include_directories(${OpenCL_INCLUDE_DIRS})
    list(APPEND EXTRA_SHARED_LIBS ${OpenCL_LIBRARIES})
endif()
