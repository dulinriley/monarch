/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

//! This build script locates CUDA libraries and headers for torch-sys-cuda,
//! which provides CUDA-specific PyTorch functionality. It depends on the base
//! torch-sys crate for core PyTorch integration.

#![feature(exit_status_error)]

#[cfg(target_os = "macos")]
fn main() {}

#[cfg(not(target_os = "macos"))]
fn main() {
    if std::env::var("CARGO_FEATURE_TENSOR_ENGINE").is_err() {
        // Do not build this module if the "tensor-engine" feature is not enabled.
        return;
    }

    // Set up Python rpath for runtime linking
    build_utils::set_python_rpath();

    // Statically link libstdc++ to avoid runtime dependency on system libstdc++
    build_utils::link_libstdcpp_static();
}
