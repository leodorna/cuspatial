# Copyright (c) 2020-2023, NVIDIA CORPORATION

from functools import cached_property
from numbers import Integral
from typing import Optional, Tuple, TypeVar, Union

import cupy as cp
import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
from geopandas.geoseries import GeoSeries as gpGeoSeries
from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)

import cudf
from cudf._typing import ColumnLike
from cudf.core.column.column import as_column

import cuspatial.io.pygeoarrow as pygeoarrow
from cuspatial.core._column.geocolumn import ColumnType, GeoColumn
from cuspatial.core._column.geometa import Feature_Enum, GeoMeta
from cuspatial.core.binpreds.binpred_dispatch import (
    CONTAINS_DISPATCH,
    COVERS_DISPATCH,
    CROSSES_DISPATCH,
    DISJOINT_DISPATCH,
    EQUALS_DISPATCH,
    INTERSECTS_DISPATCH,
    OVERLAPS_DISPATCH,
    WITHIN_DISPATCH,
)
from cuspatial.utils.column_utils import (
    contains_only_linestrings,
    contains_only_multipoints,
    contains_only_points,
    contains_only_polygons,
)

T = TypeVar("T", bound="GeoSeries")


class GeoSeries(cudf.Series):
    """cuspatial.GeoSeries enables GPU-backed storage and computation of
    shapely-like objects. Our goal is to give feature parity with GeoPandas.
    At this time, only from_geopandas and to_geopandas are directly supported.
    cuspatial GIS, indexing, and trajectory functions depend on the arrays
    stored in the `GeoArrowBuffers` object, accessible with the `points`,
    `multipoints`, `lines`, and `polygons` accessors.

    Examples
    --------

    >>> from shapely.geometry import Point
        import geopandas
        import cuspatial
        cuseries = cuspatial.GeoSeries(geopandas.GeoSeries(Point(-1, 0)))
        cuseries.points.xy
        0   -1.0
        1    0.0
        dtype: float64
    """

    def __init__(
        self,
        data: Optional[
            Union[gpd.GeoSeries, Tuple, T, pd.Series, GeoColumn, list]
        ],
        index: Union[cudf.Index, pd.Index] = None,
        dtype=None,
        name=None,
        nan_as_null=True,
    ):
        # Condition data
        if data is None or isinstance(data, (pd.Series, list)):
            data = gpGeoSeries(data)
        # Create column
        if isinstance(data, GeoColumn):
            column = data
        elif isinstance(data, GeoSeries):
            column = data._column
        elif isinstance(data, gpGeoSeries):
            from cuspatial.io.geopandas_reader import GeoPandasReader

            adapter = GeoPandasReader(data)
            pandas_meta = GeoMeta(adapter.get_geopandas_meta())
            column = GeoColumn(adapter._get_geotuple(), pandas_meta)
        else:
            raise TypeError(
                f"Incompatible object passed to GeoSeries ctor {type(data)}"
            )
        # Condition index
        if isinstance(data, (gpGeoSeries, GeoSeries)):
            if index is None:
                index = data.index
        if index is None:
            index = cudf.RangeIndex(0, len(column))
        super().__init__(
            column, index, dtype=dtype, name=name, nan_as_null=nan_as_null
        )

    @property
    def feature_types(self):
        return self._column._meta.input_types

    @property
    def type(self):
        gpu_types = cudf.Series(self._column._meta.input_types).astype("str")
        result = gpu_types.replace(
            {
                "0": "Point",
                "1": "MultiPoint",
                "2": "Linestring",
                "3": "Polygon",
            }
        )
        return result

    @property
    def column_type(self):
        """This is used to determine the type of the GeoColumn.
        It is a value returning method that produces the same result as
        the various `contains_only_*` methods, except as an Enum instead
        of many booleans."""
        if contains_only_polygons(self):
            return ColumnType.POLYGON
        elif contains_only_linestrings(self):
            return ColumnType.LINESTRING
        elif contains_only_multipoints(self):
            return ColumnType.MULTIPOINT
        elif contains_only_points(self):
            return ColumnType.POINT
        else:
            return ColumnType.MIXED

    @property
    def point_indices(self):
        if contains_only_polygons(self):
            return self.polygons.point_indices()
        elif contains_only_linestrings(self):
            return self.lines.point_indices()
        elif contains_only_multipoints(self):
            return self.multipoints.point_indices()
        elif contains_only_points(self):
            return self.points.point_indices()
        else:
            if len(self) == 0:
                return cudf.Series([0], dtype="int32")
            raise TypeError(
                "GeoSeries must contain only Points, MultiPoints, Lines, or "
                "Polygons to return point indices."
            )

    class GeoColumnAccessor:
        def __init__(self, list_series, meta):
            self._series = list_series
            self._col = self._series._column
            self._meta = meta
            self._type = Feature_Enum.POINT

        @property
        def x(self):
            return self.xy[::2].reset_index(drop=True)

        @property
        def y(self):
            return self.xy[1::2].reset_index(drop=True)

        @property
        def xy(self):
            features = self._get_current_features(self._type)
            if hasattr(features, "leaves"):
                return cudf.Series(features.leaves().values)
            else:
                return cudf.Series()

        def _get_current_features(self, type):
            # Resample the existing features so that the offsets returned
            # by `_offset` methods reflect previous slicing, and match
            # the values returned by .xy.
            existing_indices = self._meta.union_offsets[
                self._meta.input_types == type.value
            ]
            existing_features = self._col.take(existing_indices._column)
            return existing_features

        def point_indices(self):
            # Return a cupy.ndarray containing the index values that each
            # point belongs to.
            """
            offsets = cp.arange(0, len(self.xy) + 1, 2)
            sizes = offsets[1:] - offsets[:-1]
            return cp.repeat(self._series.index, sizes)
            """
            return self._meta.input_types.index[self._meta.input_types != -1]

    class MultiPointGeoColumnAccessor(GeoColumnAccessor):
        def __init__(self, list_series, meta):
            super().__init__(list_series, meta)
            self._type = Feature_Enum.MULTIPOINT

        @property
        def geometry_offset(self):
            return self._get_current_features(self._type).offsets.values

        def point_indices(self):
            # Return a cupy.ndarray containing the index values from the
            # MultiPoint GeoSeries that each individual point is member of.
            offsets = cp.array(self.geometry_offset)
            sizes = offsets[1:] - offsets[:-1]
            return cp.repeat(self._meta.input_types.index, sizes)

    class LineStringGeoColumnAccessor(GeoColumnAccessor):
        def __init__(self, list_series, meta):
            super().__init__(list_series, meta)
            self._type = Feature_Enum.LINESTRING

        @property
        def geometry_offset(self):
            return self._get_current_features(self._type).offsets.values

        @property
        def part_offset(self):
            return self._get_current_features(
                self._type
            ).elements.offsets.values

        def point_indices(self):
            # Return a cupy.ndarray containing the index values from the
            # LineString GeoSeries that each individual point is member of.
            offsets = cp.array(self.part_offset)
            sizes = offsets[1:] - offsets[:-1]
            return cp.repeat(self._meta.input_types.index, sizes)

    class PolygonGeoColumnAccessor(GeoColumnAccessor):
        def __init__(self, list_series, meta):
            super().__init__(list_series, meta)
            self._type = Feature_Enum.POLYGON

        @property
        def geometry_offset(self):
            return self._get_current_features(self._type).offsets.values

        @property
        def part_offset(self):
            return self._get_current_features(
                self._type
            ).elements.offsets.values

        @property
        def ring_offset(self):
            return self._get_current_features(
                self._type
            ).elements.elements.offsets.values

        def point_indices(self):
            # Return a cupy.ndarray containing the index values from the
            # Polygon GeoSeries that each individual point is member of.
            offsets = self.ring_offset.take(self.part_offset).take(
                self.geometry_offset
            )
            sizes = offsets[1:] - offsets[:-1]

            return self._series.index.repeat(sizes).values

    @property
    def points(self):
        """Access the `PointsArray` of the underlying `GeoArrowBuffers`."""
        return self.GeoColumnAccessor(self._column.points, self._column._meta)

    @property
    def multipoints(self):
        """Access the `MultiPointArray` of the underlying `GeoArrowBuffers`."""
        return self.MultiPointGeoColumnAccessor(
            self._column.mpoints, self._column._meta
        )

    @property
    def lines(self):
        """Access the `LineArray` of the underlying `GeoArrowBuffers`."""
        return self.LineStringGeoColumnAccessor(
            self._column.lines, self._column._meta
        )

    @property
    def polygons(self):
        """Access the `PolygonArray` of the underlying `GeoArrowBuffers`."""
        return self.PolygonGeoColumnAccessor(
            self._column.polygons, self._column._meta
        )

    def __repr__(self):
        # TODO: Implement Iloc with slices so that we can use `Series.__repr__`
        return self.to_pandas().__repr__()

    class GeoSeriesLocIndexer:
        """Map the index to an integer Series and use that."""

        def __init__(self, _sr):
            self._sr = _sr

        def __getitem__(self, item):
            booltest = cudf.Series(item)
            if booltest.dtype in (bool, np.bool_):
                return self._sr.iloc[item]

            map_df = cudf.DataFrame(
                {
                    "map": self._sr.index,
                    "idx": cp.arange(len(self._sr.index))
                    if not isinstance(item, Integral)
                    else 0,
                }
            )
            index_df = cudf.DataFrame({"map": item}).reset_index()
            new_index = index_df.merge(
                map_df, how="left", sort=False
            ).sort_values("index")
            if isinstance(item, Integral):
                return self._sr.iloc[new_index["idx"][0]]
            else:
                result = self._sr.iloc[new_index["idx"]]
                result.index = item
                return result

    class GeoSeriesILocIndexer:
        """Each row of a GeoSeries is one of the six types: Point, MultiPoint,
        LineString, MultiLineString, Polygon, or MultiPolygon.
        """

        def __init__(self, sr):
            self._sr = sr

        @cached_property
        def _type_int_to_field(self):
            return {
                Feature_Enum.POINT: self._sr.points,
                Feature_Enum.MULTIPOINT: self._sr.mpoints,
                Feature_Enum.LINESTRING: self._sr.lines,
                Feature_Enum.POLYGON: self._sr.polygons,
            }

        def __getitem__(self, indexes):
            # Slice the types and offsets
            union_offsets = self._sr._column._meta.union_offsets.iloc[indexes]
            union_types = self._sr._column._meta.input_types.iloc[indexes]

            points = self._sr._column.points
            mpoints = self._sr._column.mpoints
            lines = self._sr._column.lines
            polygons = self._sr._column.polygons

            column = GeoColumn(
                (points, mpoints, lines, polygons),
                {
                    "input_types": union_types,
                    "union_offsets": union_offsets,
                },
            )

            if isinstance(indexes, Integral):
                return GeoSeries(column, name=self._sr.name).to_shapely()
            else:
                return GeoSeries(
                    column, index=self._sr.index[indexes], name=self._sr.name
                )

    def from_arrow(union):
        column = GeoColumn(
            (
                cudf.Series(union.child(0)),
                cudf.Series(union.child(1)),
                cudf.Series(union.child(2)),
                cudf.Series(union.child(3)),
            ),
            {
                "input_types": union.type_codes,
                "union_offsets": union.offsets,
            },
        )
        return GeoSeries(column)

    @property
    def loc(self):
        """Not currently supported."""
        return self.GeoSeriesLocIndexer(self)

    @property
    def iloc(self):
        """Return the i-th row of the GeoSeries."""
        return self.GeoSeriesILocIndexer(self)

    def to_geopandas(self, nullable=False):
        """Returns a new GeoPandas GeoSeries object from the coordinates in
        the cuspatial GeoSeries.
        """
        if nullable is True:
            raise ValueError("GeoSeries doesn't support <NA> yet")
        final_union_slice = self.iloc[0 : len(self._column)]
        return gpGeoSeries(
            final_union_slice.to_shapely(),
            index=self.index.to_pandas(),
            name=self.name,
        )

    def to_pandas(self):
        """Treats to_pandas and to_geopandas as the same call, which improves
        compatibility with pandas.
        """
        return self.to_geopandas()

    def _linestring_to_shapely(self, geom):
        if len(geom) == 1:
            result = [tuple(x) for x in geom[0]]
            return LineString(result)
        else:
            linestrings = []
            for linestring in geom:
                linestrings.append(
                    LineString([tuple(child) for child in linestring])
                )
            return MultiLineString(linestrings)

    def _polygon_to_shapely(self, geom):
        if len(geom) == 1:
            rings = []
            for ring in geom[0]:
                rings.append(tuple(tuple(point) for point in ring))
            return Polygon(rings[0], rings[1:])
        else:
            polygons = []
            for p in geom:
                rings = []
                for ring in p:
                    rings.append(tuple([tuple(point) for point in ring]))
                polygons.append(Polygon(rings[0], rings[1:]))
            return MultiPolygon(polygons)

    @cached_property
    def _arrow_to_shapely(self):
        type_map = {
            Feature_Enum.NONE: lambda x: None,
            Feature_Enum.POINT: Point,
            Feature_Enum.MULTIPOINT: MultiPoint,
            Feature_Enum.LINESTRING: self._linestring_to_shapely,
            Feature_Enum.POLYGON: self._polygon_to_shapely,
        }
        return type_map

    def to_shapely(self):
        # Copy the GPU data to host for iteration and deserialization
        result_types = self._column._meta.input_types.to_arrow()

        # Get the shapely serialization methods we'll use here.
        shapely_fns = [
            self._arrow_to_shapely[Feature_Enum(x)]
            for x in result_types.to_numpy()
        ]
        union = self.to_arrow()

        # Serialize to Shapely!
        results = []
        for (result_index, shapely_serialization_fn) in zip(
            range(0, len(self)), shapely_fns
        ):
            results.append(
                shapely_serialization_fn(union[result_index].as_py())
            )

        # Finally, a slice determines that we return a list, otherwise
        # an object.
        if len(results) == 1:
            return results[0]
        else:
            return results

    def to_arrow(self):
        """Convert to a GeoArrow Array.

        Returns
        -------
        result: GeoArrow Union containing GeoArrow Arrays

        Examples
        --------
        >>> from shapely.geometry import MultiLineString, LineString
        >>> cugpdf = cuspatial.from_geopandas(geopandas.GeoSeries(
            MultiLineString(
                [
                    [(1, 0), (0, 1)],
                    [(0, 0), (1, 1)]
                ]
            )))
        >>> cugpdf.to_arrow()
        <pyarrow.lib.UnionArray object at 0x7f7061c0e0a0>
        -- is_valid: all not null
        -- type_ids:   [
            2
          ]
        -- value_offsets:   [
            0
          ]
        -- child 0 type: list<item: null>
          []
        -- child 1 type: list<item: null>
          []
        -- child 2 type: list<item: list<item: list<item: double>>>
          [
            [
              [
                [
                  1,
                  0
                ],
                [
                  0,
                  1
                ]
              ],
              [
                [
                  0,
                  0
                ],
                [
                  1,
                  1
                ]
              ]
            ]
          ]
        -- child 3 type: list<item: null>
          []
        """

        points = self._column.points
        mpoints = self._column.mpoints
        lines = self._column.lines
        polygons = self._column.polygons
        arrow_points = (
            points.to_arrow().view(pygeoarrow.ArrowPointsType)
            if len(points) > 0
            else points.to_arrow()
        )
        arrow_mpoints = (
            mpoints.to_arrow().view(pygeoarrow.ArrowMultiPointsType)
            if len(mpoints) > 1
            else mpoints.to_arrow()
        )
        arrow_lines = (
            lines.to_arrow().view(pygeoarrow.ArrowLinestringsType)
            if len(lines) > 1
            else lines.to_arrow()
        )
        arrow_polygons = (
            polygons.to_arrow().view(pygeoarrow.ArrowPolygonsType)
            if len(polygons) > 1
            else polygons.to_arrow()
        )

        return pa.UnionArray.from_dense(
            self._column._meta.input_types.to_arrow(),
            self._column._meta.union_offsets.to_arrow(),
            [
                arrow_points,
                arrow_mpoints,
                arrow_lines,
                arrow_polygons,
            ],
        )

    def _align_to_index(
        self: T,
        index: ColumnLike,
        how: str = "outer",
        sort: bool = True,
        allow_non_unique: bool = False,
    ) -> T:
        """The values in the newly aligned columns will not change,
        only positions in the union offsets and type codes.
        """
        aligned_union_offsets = (
            self._column._meta.union_offsets._align_to_index(
                index, how, sort, allow_non_unique
            )
        ).astype("int32")
        aligned_union_offsets[
            aligned_union_offsets.isna()
        ] = Feature_Enum.NONE.value
        aligned_input_types = self._column._meta.input_types._align_to_index(
            index, how, sort, allow_non_unique
        ).astype("int8")
        aligned_input_types[
            aligned_input_types.isna()
        ] = Feature_Enum.NONE.value
        column = GeoColumn(
            (
                self._column.points,
                self._column.mpoints,
                self._column.lines,
                self._column.polygons,
            ),
            {
                "input_types": aligned_input_types,
                "union_offsets": aligned_union_offsets,
            },
        )
        return GeoSeries(column)

    @classmethod
    def from_points_xy(cls, points_xy):
        """Construct a GeoSeries of POINTs from an array of interleaved xy
        coordinates.

        Parameters
        ----------
        points_xy: array-like
            Coordinates of the points, interpreted as interlaved x-y coords.

        Returns
        -------
        GeoSeries:
            A GeoSeries made of the points.
        """
        return cls(GeoColumn._from_points_xy(as_column(points_xy)))

    @classmethod
    def from_multipoints_xy(cls, multipoints_xy, geometry_offset):
        """Construct a GeoSeries of MULTIPOINTs from an array of interleaved
        xy coordinates.

        Parameters
        ----------
        points_xy: array-like
            Coordinates of the points, interpreted as interleaved x-y coords.
        geometry_offset: array-like
            Offsets indicating the starting index of the multipoint. Multiply
            the index by 2 results in the starting index of the coordinate.
            See example for detail.

        Returns
        -------
        GeoSeries:
            A GeoSeries made of the points.

        Example
        -------
        >>> import numpy as np
        >>> cuspatial.GeoSeries.from_multipoints_xy(
        ...     np.array([0, 0, 1, 1, 2, 2, 3, 3], dtype='f8'),
        ...     np.array([0, 2, 4], dtype='i4'))
        0    MULTIPOINT (0.00000 0.00000, 1.00000 1.00000)
        1    MULTIPOINT (2.00000 2.00000, 3.00000 3.00000)
        dtype: geometry
        """
        return cls(
            GeoColumn._from_multipoints_xy(
                as_column(multipoints_xy), as_column(geometry_offset)
            )
        )

    @classmethod
    def from_linestrings_xy(
        cls, linestrings_xy, part_offset, geometry_offset
    ) -> T:
        """Construct a GeoSeries of MULTILINESTRINGs from an array of
        interleaved xy coordinates.

        Parameters
        ----------
        linestrings_xy : array-like
            Coordinates of the points, interpreted as interleaved x-y coords.
        geometry_offset : array-like
            Offsets of the first coordinate of each geometry. The length of
            this array is the number of geometries.  Offsets with a difference
            greater than 1 indicate a MultiLinestring.
        part_offset : array-like
            Offsets into the coordinates array indicating the beginning of
            each part. The length of this array is the number of parts.

        Returns
        -------
        GeoSeries:
            A GeoSeries of MULTILINESTRINGs.

        Example
        -------
        >>> import cudf
        >>> import cuspatial
        >>> linestrings_xy = cudf.Series(
                [0.0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5])
        >>> part_offset = cudf.Series([0, 6])
        >>> geometry_offset = cudf.Series([0, 1])
        >>> cuspatial.GeoSeries.from_linestrings_xy(
                linestrings_xy, part_offset, geometry_offset)
        0    LINESTRING (0 0, 1 1, 2 2, 3 3, 4 4, 5 5)
        dtype: geometry
        """
        return cls(
            GeoColumn._from_linestrings_xy(
                as_column(linestrings_xy),
                as_column(part_offset, dtype="int32"),
                as_column(geometry_offset, dtype="int32"),
            )
        )

    @classmethod
    def from_polygons_xy(
        cls, polygons_xy, ring_offset, part_offset, geometry_offset
    ) -> T:
        """Construct a GeoSeries of MULTIPOLYGONs from an array of
        interleaved xy coordinates.

        Parameters
        ----------
        polygons_xy : array-like
            Coordinates of the points, interpreted as interleaved x-y coords.
        geometry_offset : array-like
            Offsets of the first coordinate of each geometry. The length of
            this array is the number of geometries.  Offsets with a difference
            greater than 1 indicate a MultiLinestring.
        part_offset : array-like
            Offsets into the coordinates array indicating the beginning of
            each part. The length of this array is the number of parts.
        rint_offset : array-like
            Offsets into the part array indicating the beginning of each ring.
            The length of this array is the number of rings.

        Returns
        -------
        GeoSeries:
            A GeoSeries of MULTIPOLYGONs.

        Example
        -------
        >>> import cudf
        >>> import cuspatial
        >>> polygons_xy = cudf.Series(
                [0.0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5])
        >>> ring_offset = cudf.Series([0, 6])
        >>> part_offset = cudf.Series([0, 1])
        >>> geometry_offset = cudf.Series([0, 1])
        >>> cuspatial.GeoSeries.from_polygons_xy(
                polygons_xy, ring_offset, part_offset, geometry_offset)
        0    POLYGON (0 0, 1 1, 2 2, 3 3, 4 4, 5 5)
        dtype: geometry
        """
        return cls(
            GeoColumn._from_polygons_xy(
                as_column(polygons_xy),
                as_column(ring_offset, dtype="int32"),
                as_column(part_offset, dtype="int32"),
                as_column(geometry_offset, dtype="int32"),
            )
        )

    def align(self, other):
        """Align the rows of two GeoSeries using outer join.

        `align` rearranges two GeoSeries so that their indices match.
        If one GeoSeries is longer than the other, the shorter GeoSeries
        will be increased in length and missing index values will be added,
        inserting `None` when an empty row is created.

        Alignment involves matching the length of the indices, sorting them,
        and inserting into the right GeoSeries extra index values that are
        present in the left GeoSeries.

        Parameters
        ----------
        other: GeoSeries

        Returns
        -------
        (left, right) : GeoSeries
            Pair of aligned GeoSeries

        Examples
        --------
        >>> points = gpd.GeoSeries([
            Point((-8, -8)),
            Point((-2, -2)),
        ])
        >>> point = gpd.GeoSeries(points[0])
        >>> print(points.align(point))

        (0    POINT (-8.00000 -8.00000)
        1    POINT (-2.00000 -2.00000)
        dtype: geometry, 0    POINT (-8.00000 -8.00000)
        1                         None
        dtype: geometry)

        >>> points_right = gpd.GeoSeries([
            Point((-2, -2)),
            Point((-8, -8)),
        ], index=[1,0])
        >>> print(points.align(points_right))

        (0    POINT (-8.00000 -8.00000)
        1    POINT (-2.00000 -2.00000)
        dtype: geometry, 0    POINT (-8.00000 -8.00000)
        1    POINT (-2.00000 -2.00000)
        dtype: geometry)

        >>> points_alpha = gpd.GeoSeries([
            Point((-1, 1)),
            Point((1, -1)),
        ], index=['a', 'b'])
        >>> print(points.align(points_alpha))

        (0    POINT (-8.00000 -8.00000)
        1    POINT (-2.00000 -2.00000)
        a                         None
        b                         None
        dtype: geometry, 0                        None
        1                        None
        a    POINT (-1.00000 1.00000)
        b    POINT (1.00000 -1.00000)
        dtype: geometry)

        """

        # Merge the two indices and sort them
        if (
            len(self.index) == len(other.index)
            and (self.index == other.index).all()
        ):
            return self, other

        idx1 = cudf.DataFrame({"idx": self.index})
        idx2 = cudf.DataFrame({"idx": other.index})
        index = idx1.merge(idx2, how="outer").sort_values("idx")["idx"]
        index.name = None

        # Align the two GeoSeries
        aligned_left = self._align_to_index(index)
        aligned_right = other._align_to_index(index)

        # If the two GeoSeries have the same length, keep the original index
        # Otherwise, use the union of the two indices
        if len(other) == len(aligned_right):
            aligned_right.index = other.index
        else:
            aligned_right.index = index
        if len(self) == len(aligned_left):
            aligned_left.index = self.index
        else:
            aligned_left.index = index

        # Aligned GeoSeries are sorted by index
        aligned_right = aligned_right.sort_index()
        aligned_left = aligned_left.sort_index()
        return (
            aligned_left,
            aligned_right,
        )

    def _gather(
        self, gather_map, keep_index=True, nullify=False, check_bounds=True
    ):
        return self.iloc[gather_map]

    # def reset_index(self, drop=False, inplace=False, name=None):
    def reset_index(
        self,
        level=None,
        drop=False,
        name=None,
        inplace=False,
    ):
        """Reset the index of the GeoSeries.

        Parameters
        ----------
        level : int, str, tuple, or list, default None
            Only remove the given levels from the index. Removes all levels
            by default.
        drop : bool, default False
            If drop is False, create a new dataframe with the original
            index as a column. If drop is True, the original index is
            dropped.
        name : object, optional
            The name to use for the column containing the original
            Series values.
        inplace: bool, default False
            If True, the original GeoSeries is modified.

        Returns
        -------
        GeoSeries
            GeoSeries with reset index.

        Examples
        --------
        >>> points = gpd.GeoSeries([
            Point((-8, -8)),
            Point((-2, -2)),
        ], index=[1, 0])
        >>> print(points.reset_index())

        0    POINT (-8.00000 -8.00000)
        1    POINT (-2.00000 -2.00000)
        dtype: geometry
        """
        geo_series = self.copy(deep=False)

        # Create a cudf series with the same index as the GeoSeries
        # and use `cudf` reset_index to identify what our result
        # should look like.
        cudf_series = cudf.Series(
            np.arange(len(geo_series.index)), index=geo_series.index
        )
        cudf_result = cudf_series.reset_index(level, drop, name, inplace)

        if not inplace:
            if isinstance(cudf_result, cudf.Series):
                geo_series.index = cudf_result.index
                return geo_series
            elif isinstance(cudf_result, cudf.DataFrame):
                # Drop was equal to False, so we need to create a
                # `GeoDataFrame` from the `GeoSeries`
                from cuspatial.core.geodataframe import GeoDataFrame

                # The columns of the `cudf.DataFrame` are the new
                # columns of the `GeoDataFrame`.
                columns = {
                    col: cudf_result[col] for col in cudf_result.columns
                }
                geo_result = GeoDataFrame(columns)
                geo_series.index = geo_result.index
                # Add the original `GeoSeries` as a column.
                if name:
                    geo_result[name] = geo_series
                else:
                    geo_result[0] = geo_series
                return geo_result
        else:
            self.index = cudf_series.index
            return None

    def contains_properly(self, other, align=False, allpairs=False):
        """Returns a `Series` of `dtype('bool')` with value `True` for each
        aligned geometry that contains _other_.

        Compute from a GeoSeries of points and a GeoSeries of polygons which
        points are properly contained within the corresponding polygon. Polygon
        A contains Point B properly if B intersects the interior of A but not
        the boundary (or exterior).

        If `allpairs=False`, the result will be a `Series` of `dtype('bool')`.
        If `allpairs=True`, the result will be a `DataFrame` containing two
        columns, `point_indices` and `polygon_indices`, each of which is a
        `Series` of `dtype('int32')`. The `point_indices` `Series` contains
        the indices of the points in the right GeoSeries, and the
        `polygon_indices` `Series` contains the indices of the polygons in the
        left GeoSeries.

        Parameters
        ----------
        other
            a cuspatial.GeoSeries
        align=True
            to align the indices before computing .contains or not. If the
            indices are not aligned, they will be compared based on their
            implicit row order.
        allpairs=False
            True computes the contains for all pairs of geometries
            between the two GeoSeries. False computes the contains for
            each geometry in the left GeoSeries against the corresponding
            geometry in the right GeoSeries. Defaults to False.

        Examples
        --------
        Test if a polygon is inside another polygon:

        >>> point = cuspatial.GeoSeries([Point(0.5, 0.5)])
        >>> polygon = cuspatial.GeoSeries([
        >>>     Polygon([[0, 0], [1, 0], [1, 1], [0, 0]]),
        >>> ])
        >>> print(polygon.contains(point))
        0    False
        dtype: bool

        Test whether three points fall within either of two polygons
        >>> point = cuspatial.GeoSeries([
        >>>     Point(0, 0),
        >>>     Point(-1, 0),
        >>>     Point(-2, 0),
        >>>     Point(0, 0),
        >>>     Point(-1, 0),
        >>>     Point(-2, 0),
        >>> ])
        >>> polygon = cuspatial.GeoSeries([
        >>>     Polygon([[0, 0], [1, 0], [1, 1], [0, 0]]),
        >>>     Polygon([[0, 0], [1, 0], [1, 1], [0, 0]]),
        >>>     Polygon([[0, 0], [1, 0], [1, 1], [0, 0]]),
        >>>     Polygon([[-2, -2], [-2, 2], [2, 2], [-2, -2]]),
        >>>     Polygon([[-2, -2], [-2, 2], [2, 2], [-2, -2]]),
        >>>     Polygon([[-2, -2], [-2, 2], [2, 2], [-2, -2]]),
        >>> ])
        >>> print(polygon.contains(point))
        0    False
        1     True
        2    False
        3    False
        4     True
        5    False
        dtype: bool

        Test whether three points fall within either of two polygons using
        `allpairs` mode:
        >>> point = cuspatial.GeoSeries([
        >>>     Point(0, 0),
        >>>     Point(-1, 0),
        >>>     Point(-2, 0),
        >>> ])
        >>> polygon = cuspatial.GeoSeries([
        >>>     Polygon([[0, 0], [1, 0], [1, 1], [0, 0]]),
        >>>     Polygon([[-2, -2], [-2, 2], [2, 2], [-2, -2]]),
        >>> ])
        >>> print(polygon.contains(point, allpairs=True))
        index point_indices  polygon_indices
        0                 2                1

        Returns
        -------
        result : cudf.Series or cudf.DataFrame
            A Series of boolean values indicating whether each point falls
            within the corresponding polygon in the input in the case of
            `allpairs=False`. A DataFrame containing two columns,
            `point_indices` and `polygon_indices`, each of which is a
            `Series` of `dtype('int32')` in the case of `allpairs=True`.
        """
        predicate = CONTAINS_DISPATCH[(self.column_type, other.column_type)](
            align=align, allpairs=allpairs
        )
        return predicate(self, other)

    def geom_equals(self, other, align=True):
        """Compute if a GeoSeries of features A is equal to a GeoSeries of
        features B. Features are equal if their vertices are equal.

        An object is equal to other if its set-theoretic boundary, interior,
        and exterior coincide with those of the other object.

        Parameters
        ----------
        other
            a cuspatial.GeoSeries
        align=True
            to align the indices before computing .contains or not. If the
            indices are not aligned, they will be compared based on their
            implicit row order.

        Examples
        --------
        Test if two points are equal:
        >>> point = cuspatial.GeoSeries([Point(0, 0)])
        >>> point2 = cuspatial.GeoSeries([Point(0, 0)])
        >>> print(point.geom_equals(point2))
        0    True
        dtype: bool

        Test if two polygons are equal:
        >>> polygon = cuspatial.GeoSeries([
        >>>     Polygon([[0, 0], [1, 0], [1, 1], [0, 0]]),
        >>> ])
        >>> polygon2 = cuspatial.GeoSeries([
        >>>     Polygon([[0, 0], [1, 0], [1, 1], [0, 0]]),
        >>> ])
        >>> print(polygon.geom_equals(polygon2))
        0    True
        dtype: bool

        Returns
        -------

        result : cudf.Series
            A Series of boolean values indicating whether each feature in A
            is equal to the corresponding feature in B.
        """
        predicate = EQUALS_DISPATCH[(self.column_type, other.column_type)](
            align=align
        )
        return predicate(self, other)

    def covers(self, other, align=True):
        """Compute if a GeoSeries of features A covers a second GeoSeries of
        features B. A covers B if no points on B lie in the exterior of A.

        Parameters
        ----------
        other
            a cuspatial.GeoSeries
        align=True
            align the GeoSeries indexes before calling the binpred

        Returns
        -------
        result : cudf.Series
            A Series of boolean values indicating whether each feature in the
            input GeoSeries covers the corresponding feature in the other
            GeoSeries.
        """
        predicate = COVERS_DISPATCH[(self.column_type, other.column_type)](
            align=align
        )
        return predicate(self, other)

    def intersects(self, other, align=True):
        """Returns a `Series` of `dtype('bool')` with value `True` for each
        aligned geometry that intersects _other_.

        An object is said to intersect _other_ if its _boundary_ and
        _interior_ intersects in any way with those of other.

        Parameters
        ----------
        other
            a cuspatial.GeoSeries
        align=True
            align the GeoSeries indexes before calling the binpred

        Returns
        -------
        result : cudf.Series
            A Series of boolean values indicating whether the geometries of
            each row intersect.
        """
        predicate = INTERSECTS_DISPATCH[(self.column_type, other.column_type)](
            align=align
        )
        return predicate(self, other)

    def within(self, other, align=True):
        """Returns a `Series` of `dtype('bool')` with value `True` for each
        aligned geometry that is within _other_.

        An object is said to be within other if at least one of its points
        is located in the interior and no points are located in the exterior
        of the other.

        Parameters
        ----------
        other
            a cuspatial.GeoSeries
        align=True
            align the GeoSeries indexes before calling the binpred

        Returns
        -------
        result : cudf.Series
            A Series of boolean values indicating whether each feature falls
            within the corresponding polygon in the input.
        """
        predicate = WITHIN_DISPATCH[(self.column_type, other.column_type)](
            align=align
        )
        return predicate(self, other)

    def overlaps(self, other, align=True):
        """Returns True for all aligned geometries that overlap other, else
        False.

        Geometries overlap if they have more than one but not all points in
        common, have the same dimension, and the intersection of the
        interiors of the geometries has the same dimension as the geometries
        themselves.

        Parameters
        ----------
        other
            a cuspatial.GeoSeries
        align=True
            align the GeoSeries indexes before calling the binpred

        Returns
        -------
        result : cudf.Series
            A Series of boolean values indicating whether each geometry
            overlaps the corresponding geometry in the input."""
        predicate = OVERLAPS_DISPATCH[(self.column_type, other.column_type)](
            align=align
        )
        return predicate(self, other)

    def crosses(self, other, align=True):
        """Returns True for all aligned geometries that cross other, else
        False.

        Geometries cross if they have some but not all interior points in
        common, have the same dimension, and the intersection of the
        interiors of the geometries has the dimension of the geometries
        themselves minus one.

        Parameters
        ----------
        other
            a cuspatial.GeoSeries
        align=True
            align the GeoSeries indexes before calling the binpred

        Returns
        -------
        result : cudf.Series
            A Series of boolean values indicating whether each geometry
            crosses the corresponding geometry in the input."""
        predicate = CROSSES_DISPATCH[(self.column_type, other.column_type)](
            align=align
        )
        return predicate(self, other)

    def disjoint(self, other, align=True):
        """Returns True for all aligned geometries that are disjoint from
        other, else False.

        An object is said to be disjoint to other if its boundary and
        interior does not intersect at all with those of the other.

        Parameters
        ----------
        other
            a cuspatial.GeoSeries
        align=True
            align the GeoSeries indexes before calling the binpred

        Returns
        -------
        result : cudf.Series
            A Series of boolean values indicating whether each pair of
            corresponding geometries is disjoint.
        """
        predicate = DISJOINT_DISPATCH[(self.column_type, other.column_type)](
            align=align
        )
        return predicate(self, other)
