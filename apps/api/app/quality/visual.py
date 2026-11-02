"""Reproducible, masked RGB pixel comparison for approved visual baselines."""

from dataclasses import dataclass
from io import BytesIO
import math

from PIL import Image, ImageChops, ImageDraw, ImageOps, UnidentifiedImageError

from apps.api.app.quality.artifacts import ArtifactStorageError, validate_png_bytes


class VisualComparisonError(ValueError):
    """A visual comparison could not be evaluated deterministically."""


@dataclass(frozen=True)
class VisualComparison:
    pixel_ratio: float
    changed_pixels: int
    compared_pixels: int
    diff_png: bytes


def normalize_mask_regions(masks):
    if not isinstance(masks, list) or len(masks) > 100:
        raise VisualComparisonError("MASK_INVALID")
    normalized = []
    for mask in masks:
        if not isinstance(mask, dict) or set(mask) != {"x", "y", "width", "height"}:
            raise VisualComparisonError("MASK_INVALID")
        values = [mask.get(key) for key in ("x", "y", "width", "height")]
        if any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
            raise VisualComparisonError("MASK_INVALID")
        x, y, width, height = (float(value) for value in values)
        if (x < 0 or y < 0 or width <= 0 or height <= 0
                or x + width > 1 or y + height > 1):
            raise VisualComparisonError("MASK_INVALID")
        normalized.append({"x": x, "y": y, "width": width, "height": height})
    return normalized


def compare_pngs(expected: bytes, actual: bytes, *, masks=None,
                 channel_tolerance: int = 0) -> VisualComparison:
    """Compare two PNG images; normalized masks are excluded from the pixel ratio."""
    try:
        expected_width, expected_height = validate_png_bytes(expected)
        actual_width, actual_height = validate_png_bytes(actual)
    except ArtifactStorageError as exc:
        raise VisualComparisonError("PNG_INVALID") from exc
    if (expected_width, expected_height) != (actual_width, actual_height):
        raise VisualComparisonError("DIMENSION_MISMATCH")
    if type(channel_tolerance) is not int or not 0 <= channel_tolerance <= 255:
        raise VisualComparisonError("CHANNEL_TOLERANCE_INVALID")
    normalized_masks = normalize_mask_regions([] if masks is None else masks)
    size = (expected_width, expected_height)
    try:
        with Image.open(BytesIO(expected)) as reference_image:
            reference = reference_image.convert("RGB")
        with Image.open(BytesIO(actual)) as actual_image:
            current = actual_image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise VisualComparisonError("PNG_INVALID") from exc

    channel_diff = ImageChops.difference(reference, current)
    changed = Image.new("L", size, 0)
    for channel in channel_diff.split():
        over_tolerance = Image.eval(channel, lambda value: 255 if value > channel_tolerance else 0)
        changed = ImageChops.lighter(changed, over_tolerance)

    comparable = Image.new("L", size, 255)
    draw = ImageDraw.Draw(comparable)
    for mask in normalized_masks:
        left = math.floor(mask["x"] * expected_width)
        top = math.floor(mask["y"] * expected_height)
        right = math.ceil((mask["x"] + mask["width"]) * expected_width) - 1
        bottom = math.ceil((mask["y"] + mask["height"]) * expected_height) - 1
        draw.rectangle((left, top, right, bottom), fill=0)

    compared_pixels = comparable.histogram()[255]
    if compared_pixels <= 0:
        raise VisualComparisonError("NO_COMPARABLE_PIXELS")
    comparable_changes = ImageChops.multiply(changed, comparable)
    changed_pixels = comparable_changes.histogram()[255]
    diff_image = ImageOps.colorize(
        comparable_changes, black=(36, 43, 55), white=(255, 48, 72))
    buffer = BytesIO()
    diff_image.save(buffer, format="PNG", optimize=False, compress_level=9)
    return VisualComparison(
        pixel_ratio=changed_pixels / compared_pixels,
        changed_pixels=changed_pixels,
        compared_pixels=compared_pixels,
        diff_png=buffer.getvalue(),
    )
