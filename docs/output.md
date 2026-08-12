# What is in `geotag.csv`

The README lists the columns. This is where each one comes from, and which of
them are ours rather than DJI's — because two columns are passed through under
DJI's own names, and one of those does not mean what its name says.

## One row is one photo

Each row is the **camera centre at the instant the shutter fired**, in the
frame CSRS-PPP solved the base in, at the epoch it solved for. Not the
antenna, not the aircraft's real-time RTK answer, and not a position DJI
wrote — every coordinate column is computed here. See
[How a camera centre is computed](pipeline.md).

## Where each column comes from

| Column | Source |
|---|---|
| `FileName` | the file on disk |
| `UTCAtExposure` | **DJI's XMP field, passed through unchanged** — see below |
| `coord_sys` | the frame the base was solved in — **replaced by the target CRS** once `transform_coordinates()` runs |
| `epoch` | the epoch the base was solved at; a transformation does not change it |
| `cam_lat`, `cam_lon`, `cam_h`, `cam_X/Y/Z` | computed — PPK trajectory interpolated to the exposure epoch, plus the MRK lever arm |
| `sigma_E`, `sigma_N`, `sigma_U` | computed — Σ<sub>PPK</sub> + Σ<sub>PPP</sub>, rotated to ENU |
| `DGT_YawDegree`, `DGT_PitchDegree`, `DGT_RollDegree` | DJI's gimbal angles, normalised — see [Camera attitude](attitude.md) |
| `rtk_status` | the MRK's quality flag, i.e. the *aircraft's* real-time solution |
| `flight` | the folder the photo came from |
| `cam_E`, `cam_N` | added by `transform_coordinates()` for a projected target |

Two of these describe the flight rather than the result. `rtk_status` is what
the aircraft achieved in real time — useful for spotting where reception was
poor, but it says nothing about the PPK solution that replaced it. `epoch`
belongs to the base, not to the photo.

**`coord_sys` is the one column that changes meaning.** Straight out of
`geotag()` it names the frame CSRS-PPP solved in — `IGb20`, say. After
`transform_coordinates()` it names the target, in full, e.g.
`NAD83(CSRS)v8 / UTM zone 12N`. That is deliberate: the file always says
which frame its numbers are in, so a transformed table cannot be mistaken for
an untransformed one. The two differ by the datum shift, which in western
Canada is over a metre — measured on one survey, 1.504 m east, 0.566 m north
and 0.299 m down between `IGb20` and `NAD83(CSRS)v8`, rigid to 0.3 mm across
992 photos.

`epoch` does not change: a transformation between frames is not an epoch
propagation. Only `resolve_base_position()` can move the epoch, and it
reports the propagation uncertainty when it does.

## `UTCAtExposure` holds GPS time, not UTC

It is DJI's field, read from the image XMP and passed through under DJI's
name. The value is **GPS time**, which in 2025 runs 18 seconds ahead of UTC.

Checked against the MRK for the same exposure, and against an independent
processor's event log:

| | |
|---|---|
| MRK record: GPS week 2376, TOW 326557.073772 | |
| → GPS time | `2025-07-23 18:42:37.073772` |
| → UTC | `2025-07-23 18:42:19.073772` |
| XMP `UTCAtExposure` | `2025-07-23T18:42:37.073772` |

The XMP value equals GPS time exactly.

The name is kept so the column can be traced back to the XMP field it came
from; renaming it would break that correspondence without fixing the value.
**Nothing in the pipeline depends on it** — exposures are matched to the
trajectory through the MRK's own GPS week and time of week, so positions are
unaffected either way.

If you need real UTC — to line the table up against a flight log, another
sensor, or anything timestamped by a computer — take `GPS_week` and
`GPS_time` from `full_output=True` and convert with the leap seconds of the
day. The reverse conversion is
[`utc2gps()`](../dji_geotagger/tools/tools.py), which uses astropy so the
leap-second table is not maintained here.

## Heights are ellipsoidal

`cam_h` is height above the ellipsoid, because that is what GNSS measures and
what CSRS-PPP returns. It is **not** orthometric height and is typically tens
of metres from it. `transform_coordinates()` will not silently convert;
`resolve_base_position(mode="manual")` refuses an orthometric height outright.

## `full_output=True`

The compact table is what a deliverable needs. `full_output=True` adds
everything the row was built from — the antenna position before the lever arm,
the lever arm itself in both frames, the full covariance matrices, the raw
EXIF and XMP fields, and `base_source`.

Reach for it when a number has to be explained rather than used: checking the
lever arm against the airframe, seeing whether a covariance was repaired,
or recovering the true UTC as above.
