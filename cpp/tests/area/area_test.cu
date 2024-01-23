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
 *
*/
#include <iostream>
#include <tuple>

#include<stdio.h>

#include <cuspatial_test/base_fixture.hpp>
#include <cuspatial_test/vector_factories.cuh>
#include <cuspatial_test/vector_equality.hpp>

#include <cuspatial/geometry/segment.cuh>
#include <cuspatial/geometry/vec_2d.hpp>

#include  <cuspatial/area.cuh>
#include <cuspatial/error.hpp>


#include <rmm/cuda_stream_view.hpp>
#include <rmm/device_scalar.hpp>
#include <rmm/device_vector.hpp>
#include <rmm/device_uvector.hpp>
#include <rmm/exec_policy.hpp>
#include <rmm/mr/device/device_memory_resource.hpp>


#include <gtest/gtest.h>

#include <initializer_list>


#include <thrust/host_vector.h>
#include <thrust/transform.h>
#include <thrust/transform_reduce.h>
#include <thrust/iterator/counting_iterator.h>

// template <typename MultiPointRange>
// __global__ void print_kernel(MultiPointRange point_rng, std::size_t n){
//   if(threadIdx.x == 0){
//     for (auto ptr = point_rng.point_begin(); ptr < point_rng.point_end(); ptr++) {
//         printf("%lf", ptr[0].x); 
//     }
//   }
// }

using namespace cuspatial;
using namespace cuspatial::test;

template <typename MultiPointRange>
struct compute_area_functor {
    using T = typename MultiPointRange::element_t;

    MultiPointRange multipoints;
    
    compute_area_functor(MultiPointRange multipoints) : multipoints(multipoints)
    {
    }

    template <typename IndexType>
    __device__ T operator()(IndexType pidx) {
      T area = 0.0;

      if(pidx < multipoints.num_points() - 1){
        vec_2d<T> point_1 = multipoints.point(pidx);
        vec_2d<T> point_2 = multipoints.point(pidx+1);
        
        area = (point_1.x*point_1.y - point_1.y*point_2.x)/2;

      }
      
      return area;
    }
};


template <typename T>
struct AreaTest : public BaseFixture {
  void run_multipolygon_area(std::initializer_list<std::size_t> multipolygon_geometry_offsets,
           std::initializer_list<std::size_t> multipolygon_part_offsets,
           std::initializer_list<std::size_t> multipolygon_ring_offsets,
           std::initializer_list<vec_2d<T>> multipolygon_coordinates,
           std::initializer_list<T> expected)
  {
      using Location = vec_2d<T>;
      auto multipolygon = make_multipolygon_array(multipolygon_geometry_offsets,
                                                  multipolygon_part_offsets,
                                                  multipolygon_ring_offsets,
                                                  multipolygon_coordinates);
      
      auto rng = multipolygon.range().as_multipoint_range();
      unsigned long int num_points = rng.num_points();
      auto out = rmm::device_vector<T>{num_points};

      compute_area_functor functor(rng);

      thrust::counting_iterator<int> iter(0);

      thrust::transform(iter, iter+rng.num_points(), out.begin(), functor);
      
      thrust::host_vector<T> h_out(out);

      for(auto it = h_out.begin(); it != h_out.end(); ++it){
        std::cout << "Element " << *it << std::endl;
      }
      // auto test = multipolygon.to_host();
      // auto [geometry_offsets, part_offsets, ring_offsets, coordinates] = multipolygon.to_host();

      // for (size_t i = 0; i < coordinates.size(); i++){
      //   std::cout << "Element at index " << i << ": " << coordinates[i] << std::endl;
      // }
      // create device vector
      // auto area = rmm::device_vector<T>({0, 1});

      // auto d_expected = make_device_uvector(expected, stream(), mr());

      // cuspatial::area(rng, area, rmm::cuda_stream_default);
      
  }
};

// float and double are logically the same but would require separate tests due to precision.
using TestTypes = ::testing::Types<float, double>;

TYPED_TEST_CASE(AreaTest, TestTypes);


TYPED_TEST(AreaTest, LinestringAreaTest)
{
  // linestring has no area
  // CUSPATIAL_RUN_TEST(this->run_multipolygon_area, 
  //                    {}, //
  //                    {},
  //                    {},
  //                    {{}},
  //                    {});   // expected areas

}


TYPED_TEST(AreaTest, PolygonSquareAreaTest)
{
  CUSPATIAL_RUN_TEST(this->run_multipolygon_area, 
                     {0, 1},    // geometry offsets
                     {0, 1},    // part offsets
                     {0, 4},    // ring offsets
                     {{0, 0}, {1, 0}, {1, 1}, {0, 0}},  // coordinates
                     {1}); // expected areas

}

TYPED_TEST(AreaTest, MultiPolygonHexagonAreaTest)
{
  CUSPATIAL_RUN_TEST(this->run_multipolygon_area, 
                     {0, 1},    // geometry offsets
                     {0, 1},    // part offsets
                     {0, 6},    // ring offsets
                     {{0, 0}, {1, 0}, {1, 1}, {-1, -2}, {0, -1}, {0, 0}},  // coordinates
                     {1}); // expected areas

}


