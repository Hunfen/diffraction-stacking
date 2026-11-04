"""The single I/O boundary for FITS files: stored bottom-up, the way FITS viewers expect.

The camera delivers sensor order (row 0 = sensor top) and the whole backend keeps that order in
memory: live previews, TIFF/PNG snapshots and intermediate arrays are never flipped. FITS viewers
(such as GIMP's FITS plug-in or ds9) draw the first data row at the bottom, so writing the array as it
comes from the sensor makes the saved frame look vertically mirrored. ``write_fits`` therefore flips
the array before writing and records ``ORIENT='FITS-BOTTOM-UP'``; ``read_fits_data`` flips it back
only when that marker is present, so files written before this convention (no marker) are still read
exactly as they were captured.

Every backend FITS write and data read must go through this module: mixing a raw ``fits.PrimaryHDU``
write with one of these readers would flip the same data twice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
from astropy.io import fits

ORIENT_KEY = "ORIENT"
ORIENT_BOTTOM_UP = "FITS-BOTTOM-UP"


def write_fits(path: str | Path, data: np.ndarray,
               entries: Iterable[tuple[str, object]] | None = None) -> None:
    """Write ``data`` (sensor order, row 0 = top) as a bottom-up FITS file.

    ``entries`` is an iterable of ``(keyword, value)`` pairs: ``COMMENT`` entries are appended (so
    several comments add up instead of overwriting each other) and every other key is assigned as a
    normal card (long names such as ``HIERARCH CAM WIDTH`` become HIERARCH cards through astropy).
    """
    flipped = np.ascontiguousarray(np.flipud(np.asarray(data)))
    hdu = fits.PrimaryHDU(flipped)
    for key, value in entries or ():
        if key == "COMMENT":
            hdu.header.add_comment(str(value))
        else:
            hdu.header[key] = value
    hdu.header[ORIENT_KEY] = ORIENT_BOTTOM_UP
    hdu.writeto(Path(path), overwrite=True)


def read_fits(path: str | Path) -> tuple[np.ndarray, fits.Header]:
    """Return ``(data, header)`` with the data back in sensor order (row 0 = sensor top)."""
    with fits.open(Path(path)) as hdul:
        data = np.array(hdul[0].data, copy=True)
        header = hdul[0].header.copy()
    if str(header.get(ORIENT_KEY, "")).strip() == ORIENT_BOTTOM_UP:
        data = np.flipud(data)
    return data, header


def read_fits_data(path: str | Path) -> np.ndarray:
    """Return only the data, oriented like the live preview (sensor order)."""
    return read_fits(path)[0]


def read_fits_header(path: str | Path) -> fits.Header:
    """Return only the primary header (no pixel data is loaded)."""
    return fits.getheader(Path(path), ext=0)
