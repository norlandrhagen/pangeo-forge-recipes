"""Standalone functions for opening sources as Dataset objects."""

import io
import tempfile
import warnings
from typing import Dict, Optional, Union

import xarray as xr
import zarr

from .patterns import FileType
from .storage import CacheFSSpecTarget, OpenFileType, _copy_btw_filesystems, _get_opener


def open_url(
    url: str,
    cache: Optional[CacheFSSpecTarget] = None,
    secrets: Optional[Dict] = None,
    open_kwargs: Optional[Dict] = None,
) -> OpenFileType:
    """Open a string-based URL with fsspec.

    :param url: The URL to be parsed by fsspec.
    :param cache: If provided, data will be cached in the object before opening.
    :param secrets: If provided these secrets will be injected into the URL as a query string.
    :param open_kwargs: Extra arguments passed to fsspec.open.
    """
    kw = open_kwargs or {}
    if cache is not None:
        # this has side effects
        cache.cache_file(url, secrets, **kw)
        open_file = cache.open_file(url, mode="rb")
    else:
        open_file = _get_opener(url, secrets, **kw)
    return open_file


OPENER_MAP = {
    FileType.netcdf3: dict(engine="scipy"),
    FileType.netcdf4: dict(engine="h5netcdf"),
    FileType.zarr: dict(engine="zarr"),
}


def _set_engine(file_type, xr_open_kwargs):
    kw = xr_open_kwargs.copy()
    if file_type == FileType.unknown:
        # Enable support for archives containing a mix of types e.g. netCDF3 and netCDF4 products
        if "engine" not in kw:
            warnings.warn(
                "Unknown file type specified without xarray engine, "
                "backend engine will be automatically selected by xarray"
            )
    elif "engine" in kw:
        engine_message_base = (
            "pangeo-forge-recipes will automatically set the xarray backend for "
            f"files of type '{file_type.value}' to '{OPENER_MAP[file_type]}', "
        )
        warn_matching_msg = engine_message_base + (
            "which is the same value you have passed via `xarray_open_kwargs`. "
            f"If this input file is actually of type '{file_type.value}', you can "
            f"remove `{{'engine': '{kw['engine']}'}}` from `xarray_open_kwargs`. "
        )
        error_mismatched_msg = engine_message_base + (
            f"which is different from the value you have passed via "
            "`xarray_open_kwargs`. If this input file is actually of type "
            f"'{file_type.value}', please remove `{{'engine': '{kw['engine']}'}}` "
            "from `xarray_open_kwargs`. "
        )
        engine_message_tail = (
            f"If this input file is not of type '{file_type.value}', please update"
            " this recipe by passing a different value to `FilePattern.file_type`."
        )
        warn_matching_msg += engine_message_tail
        error_mismatched_msg += engine_message_tail

        if kw["engine"] == OPENER_MAP[file_type]["engine"]:
            warnings.warn(warn_matching_msg)
        elif kw["engine"] != OPENER_MAP[file_type]["engine"]:
            raise ValueError(error_mismatched_msg)
    else:
        kw.update(OPENER_MAP[file_type])
    return kw


def open_with_kerchunk(
    url_or_file_obj: Union[OpenFileType, str, zarr.storage.FSStore],
    file_type: FileType = FileType.unknown,
    inline_threshold: Optional[int] = 100,
    netcdf3_max_chunk_size: Optional[int] = 100000000,
    storage_options: Optional[Dict] = None,
    grib_filters: Optional[Dict] = None,
) -> Dict:
    """Scan through item(s) with one of Kerchunk's file readers
    (SingleHdf5ToZarr, ScanGrib etc.) and create reference objects.

    :param url_or_file_obj: The url or file object to be opened.
    :param file_type: Provide this if you know what type of file it is.
    :storage_options: Storage options dict to pass to SingleHdf5ToZarr
    :grib2_open_kwargs: Extra arguments to pass to Kerchunk's ScanGrib
    """

    if isinstance(url_or_file_obj, str):
        pass
    elif isinstance(url_or_file_obj, zarr.storage.FSStore):
        if file_type is not FileType.zarr:
            raise ValueError(f"FSStore object can only be opened as FileType.zarr; got {file_type}")
    elif isinstance(url_or_file_obj, io.IOBase):
        # required to make mypy happy
        # LocalFileOpener is a subclass of io.IOBase
        pass
    elif hasattr(url_or_file_obj, "open"):
        # work around fsspec inconsistencies
        url_or_file_obj = url_or_file_obj.open()

    if file_type == FileType.netcdf4:
        from kerchunk.hdf import SingleHdf5ToZarr

        h5chunks = SingleHdf5ToZarr(
            url_or_file_obj,
            url_or_file_obj.path,
            inline_threshold=inline_threshold[0],
            storage_options=storage_options[0],
        )

        ref = h5chunks.translate()

    elif file_type == FileType.netcdf3:
        from kerchunk.netCDF3 import NetCDF3ToZarr

        chunks = NetCDF3ToZarr(
            url=url_or_file_obj,
            inline_threshold=inline_threshold[0],
            max_chunk_size=netcdf3_max_chunk_size[0],
            storage_options=storage_options[0],
        )
        ref = chunks.translate()

    elif file_type == FileType.grib:
        from kerchunk.grib2 import scan_grib

        grib_references = scan_grib(
            url=url_or_file_obj,
            inline_threshold=inline_threshold[0],
            filter=grib_filters[0],
            storage_options=storage_options[0],
        )

        # Consolidate / post-process references
        if len(grib_references) == 1:
            ref = grib_references[0]
            ref["templates"] = {"u": url_or_file_obj}
            return ref

        ref = grib_references[0].copy()
        ref["templates"] = {"u": url_or_file_obj}

        primary_refs = ref["refs"].copy()
        for _, other_ref in enumerate(grib_references[1:]):
            primary_refs.update(other_ref["refs"])
        ref["refs"] = primary_refs
    return ref


def open_with_xarray(
    url_or_file_obj: Union[OpenFileType, str, zarr.storage.FSStore],
    file_type: FileType = FileType.unknown,
    load: bool = False,
    copy_to_local=False,
    xarray_open_kwargs: Optional[Dict] = None,
) -> xr.Dataset:
    """Open item with Xarray. Accepts either fsspec open-file-like objects
    or string URLs that can be passed directly to Xarray.

    :param url_or_file_obj: The url or file object to be opened.
    :param file_type: Provide this if you know what type of file it is.
    :param load: Whether to eagerly load the data into memory ofter opening.
    :param copy_to_local: Whether to copy the file-like-object to a local path
       and pass the path to Xarray. Required for some file types (e.g. Grib).
       Can only be used with file-like-objects, not URLs.
    :xarray_open_kwargs: Extra arguments to pass to Xarray's open function.
    """
    # TODO: check file type matrix

    kw = xarray_open_kwargs or {}
    kw = _set_engine(file_type, kw)

    if copy_to_local:
        if file_type in [FileType.zarr or FileType.opendap]:
            raise ValueError(f"File type {file_type} can't be copied to a local file.")
        if isinstance(url_or_file_obj, str):
            raise ValueError(
                "Won't copy string URLs to local files. Please call ``open_url`` first."
            )
        ntf = tempfile.NamedTemporaryFile()
        tmp_name = ntf.name
        target_opener = open(tmp_name, mode="wb")
        _copy_btw_filesystems(url_or_file_obj, target_opener)
        url_or_file_obj = tmp_name

    if isinstance(url_or_file_obj, str):
        pass
    elif isinstance(url_or_file_obj, zarr.storage.FSStore):
        if file_type is not FileType.zarr:
            raise ValueError(f"FSStore object can only be opened as FileType.zarr; got {file_type}")
    elif isinstance(url_or_file_obj, io.IOBase):
        # required to make mypy happy
        # LocalFileOpener is a subclass of io.IOBase
        pass
    elif hasattr(url_or_file_obj, "open"):
        # work around fsspec inconsistencies
        url_or_file_obj = url_or_file_obj.open()
    ds = xr.open_dataset(url_or_file_obj, **kw)
    if load:
        ds.load()

    if copy_to_local and not load:
        warnings.warn(
            "Input has been copied to a local file, but the Xarray dataset has not been loaded. "
            "The data may not be accessible from other hosts. Consider adding ``load=True``."
        )

    return ds