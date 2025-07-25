/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

use cxx::ExternType;
use cxx::type_id;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde::Deserialize;
use serde::Serialize;

use crate::Layout;
use crate::bridge::ffi;

// SAFETY: This type is trival, just an i8.
unsafe impl ExternType for Layout {
    type Id = type_id!("c10::Layout");
    type Kind = cxx::kind::Trivial;
}

impl Layout {
    pub(crate) fn from_py_object_or_none(obj: &Bound<'_, PyAny>) -> Option<Self> {
        ffi::py_object_is_layout(obj.clone().into())
            .then(|| ffi::layout_from_py_object(obj.into()).unwrap())
    }
}

impl FromPyObject<'_> for Layout {
    fn extract_bound(obj: &Bound<'_, PyAny>) -> PyResult<Self> {
        ffi::layout_from_py_object(obj.into()).map_err(|e| {
            PyValueError::new_err(format!(
                "Failed extracting {} from py as Layout: {}",
                obj, e
            ))
        })
    }
}

impl<'py> IntoPyObject<'py> for Layout {
    type Target = PyAny;
    type Output = Bound<'py, Self::Target>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        ffi::layout_to_py_object(self).into_pyobject(py)
    }
}

// Remotely implement Serialize/Deserialize for generated types
// TODO: we should be able to use parse_callbacks + add_derives, (see
// https://github.com/rust-lang/rust-bindgen/pull/2059) and avoid a remote
// implementation, but this is not supported by rust_bindgen_library.
#[derive(Serialize, Deserialize)]
#[serde(remote = "Layout")]
pub struct LayoutDef(i8);

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Layout;

    #[test]
    fn convert_to_py_and_back() {
        pyo3::prepare_freethreaded_python();
        let layout = Layout::Strided;
        let converted_type = Python::with_gil(|py| {
            // import torch to ensure torch.layout types are registered
            py.import("torch").unwrap();
            let obj = layout.into_pyobject(py).unwrap();
            obj.extract::<Layout>().unwrap()
        });
        assert_eq!(converted_type, Layout::Strided);
    }

    #[test]
    fn from_py() {
        pyo3::prepare_freethreaded_python();
        let layout = Python::with_gil(|py| {
            let obj = py.import("torch").unwrap().getattr("strided").unwrap();
            obj.extract::<Layout>().unwrap()
        });
        assert_eq!(layout, Layout::Strided);
    }
}
