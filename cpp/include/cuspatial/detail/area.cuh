/*
 * Copyright (c) 2022, NVIDIA CORPORATION.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#pragma once

#include <iostream>

#include <cuspatial/constants.hpp>
#include <cuspatial/error.hpp>
#include <cuspatial/geometry/vec_2d.hpp>
#include <cuspatial/traits.hpp>

#include <rmm/cuda_stream_view.hpp>
#include <rmm/exec_policy.hpp>

#include <type_traits>

namespace cuspatial {

namespace detail {

template <typename T>
struct area_functor{

    __device__ T operator()(vec_2d<T> lonlat_a, vec_2d<T> lonlat_b)
    {
        auto ax = lonlat_a.x;
        auto ay = lonlat_a.y;
        auto bx = lonlat_b.x;
        auto by = lonlat_b.y;
    }


};

}  // namespace detail

template <class MultiPolygon_Rng, class OutputIt>
OutputIt area(MultiPolygon_Rng multipolygons_rng,
              OutputIt area,
              rmm::cuda_stream_view stream)
{
    auto point_rng = multipolygons_rng[0].as_multipoint_range();

    for (auto ptr = point_rng.begin(); ptr < point_rng.end(); ptr++) {
        std::cout << *ptr << " "; 
    }
}

} // namespace cuspatial