/*
 * Copyright (c) 2022-2023, NVIDIA CORPORATION.
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

 #include <cuspatial/area.hpp>
 #include <cuspatial/error.hpp>

#include <cudf_test/base_fixture.hpp>
#include <cudf_test/column_utilities.hpp>
#include <cudf_test/column_wrapper.hpp>
#include <cudf_test/type_lists.hpp>

#include <type_traits>


//  TYPED_TEST(HaversineTest, EquivalentPoints)
// {
//   using T = TypeParam;

//   auto a_lon = fixed_width_column_wrapper<T>({-180, 180});
//   auto a_lat = fixed_width_column_wrapper<T>({0, 30});
//   auto b_lon = fixed_width_column_wrapper<T>({180, -180});
//   auto b_lat = fixed_width_column_wrapper<T>({0, 30});

//   auto expected = fixed_width_column_wrapper<T>({1.5604449514735574e-12, 1.3513849691832763e-12});

//   auto actual = cuspatial::haversine_distance(a_lon, a_lat, b_lon, b_lat);

//   CUDF_TEST_EXPECT_COLUMNS_EQUAL(expected, actual->view(), verbosity);
// }
