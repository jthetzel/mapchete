#!/usr/bin/env python

import numpy as np
import numpy.ma as ma
import os
from copy import deepcopy
from rasterio.warp import transform_bounds
import rasterio
import fiona
from tempfile import NamedTemporaryFile
from itertools import chain
from geoalchemy2.shape import from_shape
from sqlalchemy import (
    create_engine,
    Table,
    MetaData,
    and_
    )
from sqlalchemy.orm import *
from sqlalchemy.pool import NullPool
from shapely.geometry import shape
import warnings

from tilematrix import *
from .numpy_io import write_numpy, read_numpy


class VectorProcessTile(object):
    """
    Class representing a tile (existing or virtual) of target pyramid from a
    Mapchete process output.
    """
    def __init__(
        self,
        input_mapchete,
        tile,
        pixelbuffer=0,
        ):

        try:
            assert os.path.isfile(input_mapchete.config.process_file)
        except:
            raise IOError("input file does not exist: %s" %
                input_mapchete.config.process_file)

        try:
            assert pixelbuffer >= 0
        except:
            raise ValueError("pixelbuffer must be 0 or greater")

        try:
            assert isinstance(pixelbuffer, int)
        except:
            raise ValueError("pixelbuffer must be an integer")

        self.process = input_mapchete
        self.tile_pyramid = self.process.tile_pyramid
        self.tile = tile
        self.input_file = input_mapchete
        self.pixelbuffer = pixelbuffer
        self.schema = self.process.output.schema
        self.driver = self.process.output.driver
        self.crs = self.tile_pyramid.crs

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        # TODO cleanup
        pass

    def read(self, no_neighbors=False, from_baselevel=False):
        """
        Returns features of all underlying tiles. If no_neighbors is set True,
        only the base tile is returned (ATTENTION: won't work if the parent
        mapchete process has a different metatile setting).
        """

        if no_neighbors:
            tile = self.process.tile(self.tile)
            if tile.exists():
                return read_vector_window(
                    tile.path,
                    tile,
                    pixelbuffer=self.pixelbuffer
                )
            else:
                return []
        else:
            dst_tile_bbox = self.tile.bbox(pixelbuffer=self.pixelbuffer)
            src_tiles = [
                self.process.tile(tile)
                for tile in self.process.tile_pyramid.tiles_from_bbox(
                    dst_tile_bbox,
                    self.tile.zoom
                )
                ]

            return chain.from_iterable(
                read_vector_window(
                    tile.path,
                    tile,
                    pixelbuffer=self.pixelbuffer
                )
                for tile in src_tiles
                if tile.exists()
            )

    def is_empty(self, indexes=None):
        """
        Returns true if no tiles are available.
        """
        dst_tile_bbox = self.tile.bbox(pixelbuffer=self.pixelbuffer)
        src_tiles = [
            self.process.tile(tile)
            for tile in self.process.tile_pyramid.tiles_from_bbox(
                dst_tile_bbox,
                self.tile.zoom
            )
        ]

        tile_paths = [
            tile.path
            for tile in src_tiles
            if tile.exists()
            ]

        if tile_paths:
            return False
        else:
            return True


class VectorFileTile(object):
    """
    Class representing a reprojected subset of an input vector dataset clipped
    to the tile boundaries. read() returns a Fiona-like dictionary with a
    "geometry" and a "properties" field.
    """

    def __init__(
        self,
        input_file,
        tile,
        pixelbuffer=0
        ):
        try:
            assert os.path.isfile(input_file)
        except:
            raise IOError("input file does not exist: %s" % input_file)

        try:
            assert pixelbuffer >= 0
        except:
            raise ValueError("pixelbuffer must be 0 or greater")

        try:
            assert isinstance(pixelbuffer, int)
        except:
            raise ValueError("pixelbuffer must be an integer")

        try:
            self.process = tile.process
        except:
            self.process = None
        self.tile_pyramid = tile.tile_pyramid
        self.tile = tile
        self.input_file = input_file
        self.pixelbuffer = pixelbuffer
        self.crs = self.tile_pyramid.crs

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        # TODO cleanup
        pass

    def read(self):
        """
        This is a wrapper around the read_vector_window function of tilematrix.
        Tilematrix itself uses fiona to read vector data.
        This function returns a generator of GeoJSON-like dictionaries
        containing the clipped vector data and attributes.
        """
        return read_vector_window(
            self.input_file,
            self.tile,
            pixelbuffer=self.pixelbuffer
        )

    def is_empty(self, indexes=None):
        """
        Returns true if input is empty.
        """
        src_bbox = file_bbox(self.input_file, self.tile_pyramid)
        tile_geom = self.tile.bbox(pixelbuffer=self.pixelbuffer)
        if not tile_geom.intersects(src_bbox):
            return True

        # Reproject tile bounds to source file SRS.
        src_left, src_bottom, src_right, src_top = transform_bounds(
            self.tile.crs,
            self.crs,
            *self.tile.bounds(pixelbuffer=self.pixelbuffer),
            densify_pts=21
            )

        with fiona.open(self.input_file, 'r') as vector:
            features = vector.filter(
                bbox=self.tile.bounds(pixelbuffer=self.pixelbuffer)
            )
            try:
                next(features)
            except StopIteration:
                return True
            except:
                raise
            return False

    def _read_metadata(self):
        """
        Returns a rasterio-like metadata dictionary adapted to tile.
        """
        with rasterio.open(self.input_file, "r") as src:
            out_meta = deepcopy(src.meta)
        # create geotransform
        px_size = self.tile_pyramid.pixel_x_size(self.tile.zoom)
        left, bottom, right, top = self.tile.bounds(
            pixelbuffer=self.pixelbuffer
            )
        tile_geotransform = (left, px_size, 0.0, top, 0.0, -px_size)
        out_meta.update(
            width=self.tile_pyramid.tile_size,
            height=self.tile_pyramid.tile_size,
            transform=tile_geotransform,
            affine=self.tile.affine(pixelbuffer=self.pixelbuffer)
        )
        return out_meta


class RasterProcessTile(object):
    """
    Class representing a tile (existing or virtual) of target pyramid from a
    Mapchete process output.
    """
    def __init__(
        self,
        input_mapchete,
        tile,
        pixelbuffer=0,
        resampling="nearest"
        ):

        try:
            assert os.path.isfile(input_mapchete.config.process_file)
        except:
            raise IOError("input file does not exist: %s" %
                input_mapchete.config.process_file)

        try:
            assert pixelbuffer >= 0
        except:
            raise ValueError("pixelbuffer must be 0 or greater")

        try:
            assert isinstance(pixelbuffer, int)
        except:
            raise ValueError("pixelbuffer must be an integer")

        try:
            assert resampling in RESAMPLING_METHODS
        except:
            raise ValueError("resampling method %s not found." % resampling)

        # try:
        #     assert tile.process
        # except:
        #     raise ValueError("please provide an input process")
        self.process = input_mapchete
        self.tile_pyramid = self.process.tile_pyramid
        self.tile = tile
        self.input_file = input_mapchete
        self.pixelbuffer = pixelbuffer
        self.resampling = resampling
        self.profile = self._read_metadata()
        self.affine = self.profile["affine"]
        self.nodata = self.profile["nodata"]
        self.indexes = self.profile["count"]
        self.dtype = self.profile["dtype"]
        self.crs = self.tile_pyramid.crs
        self.shape = (self.profile["width"], self.profile["height"])

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        # TODO cleanup
        pass

    def _read_metadata(self):
        """
        Returns a rasterio-like metadata dictionary adapted to tile.
        """
        out_meta = self.process.output.profile
        # create geotransform
        px_size = self.tile_pyramid.pixel_x_size(self.tile.zoom)
        left, bottom, right, top = self.tile.bounds(
            pixelbuffer=self.pixelbuffer
            )
        tile_geotransform = (left, px_size, 0.0, top, 0.0, -px_size)
        out_meta.update(
            width=self.tile.width+2*self.pixelbuffer,
            height=self.tile.height+2*self.pixelbuffer,
            transform=tile_geotransform,
            affine=self.tile.affine(pixelbuffer=self.pixelbuffer)
        )
        return out_meta

    def read(self, indexes=None, from_baselevel=False):
        """
        Generates numpy arrays from input process bands.
        - dst_tile: this tile (self.tile)
        - src_tile(s): original MapcheteProcess pyramid tile
        Note: this is a semi-hacky variation as it uses an os.system call to
        generate a temporal mosaic using the gdalbuildvrt command.
        """
        if indexes:
            if isinstance(indexes, list):
                band_indexes = indexes
            else:
                band_indexes = [indexes]
        else:
            band_indexes = range(1, self.indexes+1)

        dst_tile_bbox = self.tile.bbox(pixelbuffer=self.pixelbuffer)
        src_tiles = [
            self.process.tile(tile)
            for tile in self.process.tile_pyramid.tiles_from_bbox(
                dst_tile_bbox,
                self.tile.zoom
            )
        ]
        # TODO flesh out mosaic_tiles() function and reimplement using internal
        # numpy arrays.
        tile_paths = [
            tile.path
            for tile in src_tiles
            if tile.exists()
            ]
        if len(tile_paths) == 0:
            # return emtpy array if no input files are given
            empty_array =  ma.masked_array(
                ma.zeros(
                    self.shape,
                    dtype=self.dtype
                ),
                mask=True
                )
            return [
                empty_array
                for index in band_indexes
            ]

        temp_vrt = NamedTemporaryFile()
        build_vrt = "gdalbuildvrt %s %s > /dev/null" %(
            temp_vrt.name,
            ' '.join(tile_paths)
            )
        try:
            os.system(build_vrt)
            return list(read_raster_window(
                temp_vrt.name,
                self.tile,
                indexes=band_indexes,
                pixelbuffer=self.pixelbuffer,
                resampling=self.resampling
            ))
        except:
            raise
        # finally:
            # clean up
            # if os.path.isfile(temp_vrt.name):
            #     os.remove(temp_vrt.name)

    def is_empty(self, indexes=None):
        """
        Returns true if all items are masked.
        """
        src_bbox = self.input_file.config.process_area(self.tile.zoom)
        tile_geom = self.tile.bbox(
            pixelbuffer=self.pixelbuffer
        )
        if not tile_geom.intersects(src_bbox):
            return True

        if indexes:
            if isinstance(indexes, list):
                band_indexes = indexes
            else:
                band_indexes = [indexes]
        else:
            band_indexes = range(1, self.indexes+1)

        dst_tile_bbox = self.tile.bbox(pixelbuffer=self.pixelbuffer)
        src_tiles = [
            self.process.tile(tile)
            for tile in self.process.tile_pyramid.tiles_from_bbox(
                dst_tile_bbox,
                self.tile.zoom
            )
        ]

        temp_vrt = NamedTemporaryFile()
        tile_paths = [
            tile.path
            for tile in src_tiles
            if tile.exists()
            ]

        if len(tile_paths) == 0:
            return True

        build_vrt = "gdalbuildvrt %s %s > /dev/null" %(
            temp_vrt.name,
            ' '.join(tile_paths)
            )
        try:
            os.system(build_vrt)
        except:
            raise IOError((tile.id, "failed", "build temporary VRT failed"))

        all_bands_empty = True
        for band in self.read(band_indexes):
            if not band.mask.all():
                all_bands_empty = False
                break

        return all_bands_empty


class NumpyTile(object):
    """
    Class representing a tile (existing or virtual) of target pyramid from a
    Mapchete NumPy process output.
    """
    def __init__(
        self,
        input_mapchete,
        tile,
        pixelbuffer=0,
        resampling="nearest"
        ):

        try:
            assert os.path.isfile(input_mapchete.config.process_file)
        except:
            raise IOError("input file does not exist: %s" %
                input_mapchete.config.process_file)

        try:
            assert pixelbuffer == 0
        except:
            raise NotImplementedError(
                "pixelbuffers for NumPy data not yet supported"
            )

        try:
            assert isinstance(pixelbuffer, int)
        except:
            raise ValueError("pixelbuffer must be an integer")

        try:
            assert resampling in RESAMPLING_METHODS
        except:
            raise ValueError("resampling method %s not found." % resampling)

        self.process = input_mapchete
        self.tile_pyramid = self.process.tile_pyramid
        self.tile = tile
        self.input_file = input_mapchete
        self.pixelbuffer = pixelbuffer
        self.resampling = resampling
        self.profile = self._read_metadata()
        self.affine = self.profile["affine"]
        self.nodata = self.profile["nodata"]
        self.indexes = self.profile["count"]
        self.dtype = self.profile["dtype"]
        self.crs = self.tile_pyramid.crs
        self.shape = (self.profile["width"], self.profile["height"])

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        # TODO cleanup
        pass

    def _read_metadata(self):
        """
        Returns a rasterio-like metadata dictionary adapted to tile.
        """
        out_meta = self.process.output.profile
        # create geotransform
        px_size = self.tile_pyramid.pixel_x_size(self.tile.zoom)
        left, bottom, right, top = self.tile.bounds(
            pixelbuffer=self.pixelbuffer
            )
        tile_geotransform = (left, px_size, 0.0, top, 0.0, -px_size)
        out_meta.update(
            width=self.tile.width+2*self.pixelbuffer,
            height=self.tile.height+2*self.pixelbuffer,
            transform=tile_geotransform,
            affine=self.tile.affine(pixelbuffer=self.pixelbuffer)
        )
        return out_meta

    def read(self, indexes=None, from_baselevel=False):
        """
        Generates numpy arrays from input process bands.
        - dst_tile: this tile (self.tile)
        - src_tile(s): original MapcheteProcess pyramid tile
        Note: this is a semi-hacky variation as it uses an os.system call to
        generate a temporal mosaic using the gdalbuildvrt command.
        """
        if indexes:
            if isinstance(indexes, list):
                band_indexes = indexes
            else:
                band_indexes = [indexes]
        else:
            band_indexes = range(1, self.indexes+1)

        tile = self.process.tile(self.tile)

        if tile.exists():
            return read_numpy(tile.path)
        else:
            return "herbert"
        #
        # else:
        #     empty_array =  ma.masked_array(
        #         ma.zeros(
        #             self.shape,
        #             dtype=self.dtype
        #         ),
        #         mask=True
        #         )
        #     return (
        #         empty_array
        #     )


    def is_empty(self, indexes=None):
        """
        Returns true if all items are masked.
        """
        src_bbox = self.input_file.config.process_area(self.tile.zoom)
        tile_geom = self.tile.bbox(
            pixelbuffer=self.pixelbuffer
        )
        if not tile_geom.intersects(src_bbox):
            return True

        if indexes:
            if isinstance(indexes, list):
                band_indexes = indexes
            else:
                band_indexes = [indexes]
        else:
            band_indexes = range(1, self.indexes+1)

        tile = self.process.tile(self.tile)

        if not tile.exists():
            return True
        else:
            return False

        all_bands_empty = True
        for band in self.read(band_indexes):
            if not isinstance(band, ma.MaskedArray):
                all_bands_empty = False
                break
            if not band.mask.all():
                all_bands_empty = False
                break

        return all_bands_empty


class RasterFileTile(object):
    """
    Class representing a reprojected and resampled version of an original file
    to a given tile pyramid tile. Properties and functions are inspired by
    rasterio's way of handling datasets.
    """

    def __init__(
        self,
        input_file,
        tile,
        pixelbuffer=0,
        resampling="nearest"
        ):
        try:
            assert os.path.isfile(input_file)
        except:
            raise IOError("input file does not exist: %s" % input_file)

        try:
            assert pixelbuffer >= 0
        except:
            raise ValueError("pixelbuffer must be 0 or greater")

        try:
            assert isinstance(pixelbuffer, int)
        except:
            raise ValueError("pixelbuffer must be an integer")

        try:
            assert resampling in RESAMPLING_METHODS
        except:
            raise ValueError("resampling method %s not found." % resampling)

        try:
            self.process = tile.process
        except:
            self.process = None
        self.tile_pyramid = tile.tile_pyramid
        self.tile = tile
        self.input_file = input_file
        self.pixelbuffer = pixelbuffer
        self.resampling = resampling
        self.profile = self._read_metadata()
        self.affine = self.profile["affine"]
        self.nodata = self.profile["nodata"]
        self.indexes = self.profile["count"]
        self.dtype = self.profile["dtype"]
        self.crs = self.tile_pyramid.crs
        self.shape = (self.profile["width"], self.profile["height"])

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        # TODO cleanup
        pass

    def read(self, indexes=None):
        """
        Generates numpy arrays from input bands.
        """
        if indexes:
            if isinstance(indexes, list):
                band_indexes = indexes
            else:
                band_indexes = [indexes]
        else:
            band_indexes = range(1, self.indexes+1)

        return read_raster_window(
            self.input_file,
            self.tile,
            indexes=band_indexes,
            pixelbuffer=self.pixelbuffer,
            resampling=self.resampling
        )


    def is_empty(self, indexes=None):
        """
        Returns true if all items are masked.
        """
        src_bbox = file_bbox(self.input_file, self.tile_pyramid)
        tile_geom = self.tile.bbox(pixelbuffer=self.pixelbuffer)
        if not tile_geom.intersects(src_bbox):
            return True

        if indexes:
            if isinstance(indexes, list):
                band_indexes = indexes
            else:
                band_indexes = [indexes]
        else:
            band_indexes = range(1, self.indexes+1)

        with rasterio.open(self.input_file, "r") as src:

            # Reproject tile bounds to source file SRS.
            src_left, src_bottom, src_right, src_top = transform_bounds(
            self.tile.crs,
            src.crs,
            *self.tile.bounds(pixelbuffer=self.pixelbuffer),
            densify_pts=21
            )

            minrow, mincol = src.index(src_left, src_top)
            maxrow, maxcol = src.index(src_right, src_bottom)

            # Calculate new Affine object for read window.
            window = (minrow, maxrow), (mincol, maxcol)
            window_vector_affine = src.affine.translation(
                mincol,
                minrow
                )
            window_affine = src.affine * window_vector_affine
            # Finally read data per band and store it in tuple.
            bands = (
                src.read(index, window=window, masked=True, boundless=True)
                for index in band_indexes
                )

            all_bands_empty = True
            for band in bands:
                if not band.mask.all():
                    all_bands_empty = False
                    break

            return all_bands_empty


    def _read_metadata(self):
        """
        Returns a rasterio-like metadata dictionary adapted to tile.
        """
        with rasterio.open(self.input_file, "r") as src:
            out_meta = deepcopy(src.meta)
        # create geotransform
        px_size = self.tile_pyramid.pixel_x_size(self.tile.zoom)
        left, bottom, right, top = self.tile.bounds(
            pixelbuffer=self.pixelbuffer
            )
        tile_geotransform = (left, px_size, 0.0, top, 0.0, -px_size)
        out_meta.update(
            width=self.tile_pyramid.tile_size,
            height=self.tile_pyramid.tile_size,
            transform=tile_geotransform,
            affine=self.tile.affine(pixelbuffer=self.pixelbuffer)
        )
        return out_meta

def mosaic_tiles(
    src_tiles,
    indexes=None
    ):
    """
    Returns a larger numpy array of input tiles.
    """
    if indexes:
        if isinstance(indexes, list):
            band_indexes = indexes
        else:
            band_indexes = [indexes]
    else:
        band_indexes = range(1, self.indexes+1)


def write_raster(
    process,
    metadata,
    bands,
    pixelbuffer=0
    ):

    try:
        assert isinstance(bands, tuple) or isinstance(bands, np.ndarray)
    except:
        raise TypeError(
            "output bands must be stored in a tuple or a numpy array."
        )

    # try:
    #     assert isinstance(bands, tuple)
    # except:
    #     try:
    #         assert (
    #             isinstance(
    #             bands,
    #             np.ndarray
    #             ) or isinstance(
    #             bands,
    #             np.ma.core.MaskedArray
    #             )
    #         )
    #         bands = (bands, )
    #     except:
    #         raise TypeError("output bands must be stored in a tuple.")

    try:
        for band in bands:
            assert (
                isinstance(
                    band,
                    np.ndarray
                ) or isinstance(
                    band,
                    np.ma.core.MaskedArray
                )
            )
    except:
        raise TypeError(
            "output bands must be numpy ndarrays, not %s" % type(band)
            )

    process.tile.prepare_paths()

    if process.output.format == "NumPy":
        try:
            write_numpy(
                process.tile,
                metadata,
                bands,
                pixelbuffer=pixelbuffer
            )
        except:
            raise
    else:
        try:
            for band in bands:
                assert band.ndim == 2
        except:
            raise TypeError(
                "output bands must be 2-dimensional, not %s" % band.ndim
                )
        try:
            write_raster_window(
                process.tile.path,
                process.tile,
                metadata,
                bands,
                pixelbuffer=pixelbuffer
            )
        except:
            raise


def write_vector(
    process,
    metadata,
    data,
    pixelbuffer=0,
    overwrite=False
    ):
    assert isinstance(metadata["output"].schema, dict)
    assert isinstance(metadata["output"].driver, str)
    assert isinstance(data, list)

    if process.output.is_db:

        config = process.config.at_zoom(process.tile.zoom)

        # connect to db
        db_url = 'postgresql://%s:%s@%s:%s/%s' %(
            metadata["output"].db_params["user"],
            metadata["output"].db_params["password"],
            metadata["output"].db_params["host"],
            metadata["output"].db_params["port"],
            metadata["output"].db_params["db"]
        )
        engine = create_engine(db_url, poolclass=NullPool)
        meta = MetaData()
        meta.reflect(bind=engine)
        TargetTable = Table(
            metadata["output"].db_params["table"],
            meta,
            autoload=True,
            autoload_with=engine
        )
        Session = sessionmaker(bind=engine)
        session = Session()

        if overwrite:
            delete_old = TargetTable.delete(and_(
                TargetTable.c.zoom == process.tile.zoom,
                TargetTable.c.row == process.tile.row,
                TargetTable.c.col == process.tile.col)
                )
            session.execute(delete_old)

        for feature in data:
            try:
                raw_geom = feature["geometry"]
                geom = from_shape(
                    shape(feature["geometry"]).intersection(
                        process.tile.bbox(pixelbuffer=pixelbuffer)
                    ),
                    srid=process.tile.srid
                )
                # else:
                #     continue
            except Exception as e:
                warnings.warn("corrupt geometry: %s" %(e))
                continue

            properties = {}
            properties.update(
                zoom=process.tile.zoom,
                row=process.tile.row,
                col=process.tile.col,
                geom=geom
            )
            properties.update(feature["properties"])

            insert = TargetTable.insert().values(properties)
            session.execute(insert)

        session.commit()
        session.close()
        engine.dispose()

    else:
        process.tile.prepare_paths()

        if process.tile.exists():
            os.remove(process.tile.path)

        try:
            write_vector_window(
                process.tile.path,
                process.tile,
                metadata,
                data,
                pixelbuffer=pixelbuffer
            )
        except:
            if process.tile.exists():
                os.remove(process.tile.path)
            raise


def read_vector(
    process,
    input_file,
    pixelbuffer=0
    ):
    """
    This is a wrapper around the read_vector_window function of tilematrix.
    Tilematrix itself uses fiona to read vector data.
    This function returns a list of GeoJSON-like dictionaries containing the
    clipped vector data and attributes.
    """
    if input_file:
        features = read_vector_window(
            input_file,
            process.tile,
            pixelbuffer=pixelbuffer
        )
    else:
        features = None

    return features
