from io import BytesIO

import pytest
from PIL import Image

from apps.api.app.quality.visual import VisualComparisonError, compare_pngs


def png(rows):
    image = Image.new("RGB", (len(rows[0]), len(rows)))
    image.putdata([pixel for row in rows for pixel in row])
    stream = BytesIO()
    image.save(stream, format="PNG", optimize=False, compress_level=9)
    return stream.getvalue()


def test_identical_images_have_zero_ratio_and_reproducible_diff():
    source = png([[(12, 20, 30), (40, 50, 60)], [(70, 80, 90), (100, 110, 120)]])

    first = compare_pngs(source, source)
    second = compare_pngs(source, source)

    assert first.pixel_ratio == 0
    assert first.changed_pixels == 0
    assert first.compared_pixels == 4
    assert first.diff_png == second.diff_png


def test_changed_pixels_are_counted_deterministically():
    expected = png([[(0, 0, 0), (0, 0, 0)], [(0, 0, 0), (0, 0, 0)]])
    actual = png([[(255, 255, 255), (0, 0, 0)], [(0, 0, 0), (0, 0, 0)]])

    result = compare_pngs(expected, actual)

    assert result.pixel_ratio == 0.25
    assert result.changed_pixels == 1
    assert result.compared_pixels == 4


def test_masked_pixels_are_removed_from_the_comparison_denominator():
    expected = png([[(0, 0, 0), (0, 0, 0)], [(0, 0, 0), (0, 0, 0)]])
    actual = png([[(255, 255, 255), (0, 0, 0)], [(0, 0, 0), (0, 0, 0)]])

    result = compare_pngs(expected, actual, masks=[{"x": 0, "y": 0, "width": 0.5, "height": 0.5}])

    assert result.pixel_ratio == 0
    assert result.changed_pixels == 0
    assert result.compared_pixels == 3


def test_channel_tolerance_is_applied_per_pixel():
    expected = png([[(10, 10, 10)]])
    actual = png([[(13, 10, 10)]])

    assert compare_pngs(expected, actual, channel_tolerance=3).changed_pixels == 0
    assert compare_pngs(expected, actual, channel_tolerance=2).changed_pixels == 1


def test_dimension_mismatch_fails_closed():
    with pytest.raises(VisualComparisonError, match="DIMENSION_MISMATCH"):
        compare_pngs(png([[(0, 0, 0)]]), png([[(0, 0, 0), (0, 0, 0)]]))


@pytest.mark.parametrize("mask", [
    {"x": -0.1, "y": 0, "width": 0.2, "height": 0.2},
    {"x": 0.9, "y": 0.9, "width": 0.2, "height": 0.2},
    {"x": 0, "y": 0, "width": 0, "height": 0.2},
    {"x": True, "y": 0, "width": 0.2, "height": 0.2},
])
def test_invalid_masks_are_rejected(mask):
    source = png([[(0, 0, 0)]])
    with pytest.raises(VisualComparisonError, match="MASK_INVALID"):
        compare_pngs(source, source, masks=[mask])
