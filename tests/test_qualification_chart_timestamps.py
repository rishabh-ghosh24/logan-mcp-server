"""OCI timestamp columns must produce readable, correctly scaled time axes."""

import base64

import matplotlib.dates as mdates
import matplotlib
import pytest

from oci_logan_mcp.visualization import ChartType, VisualizationEngine


def render_with_axes(monkeypatch, chart_type, *, timestamp_type="TIMESTAMP", multiple=False):
    engine = VisualizationEngine()
    figures = []
    encode = engine._fig_to_base64

    def capture(fig):
        fig.canvas.draw()
        axis = fig.axes[0]
        figures.append({"xlabels": [label.get_text() for label in axis.get_xticklabels()],
                        "ylabels": [label.get_text() for label in axis.get_yticklabels()],
                        "offset": axis.xaxis.get_offset_text().get_text(),
                        "dates": list(axis.lines[0].get_xdata(orig=False)) if axis.lines else []})
        return encode(fig)

    monkeypatch.setattr(engine, "_fig_to_base64", capture)
    data = {"columns": [{"name": "Time", "type": timestamp_type}, {"name": "Count", "type": "LONG"}],
            "rows": [[1767225600000, 10], [1767225900000, 20], [1767226500000, 30]]}
    if multiple:
        data["columns"].append({"name": "Errors", "type": "LONG"})
        for row in data["rows"]:
            row.append(1)
    result = engine.generate(data, chart_type)
    assert base64.b64decode(result["image_base64"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert result["raw_data"][0]["Time"] == 1767225600000
    assert data["rows"][0][0] == 1767225600000
    assert len(figures) == 1
    return figures[0]


@pytest.mark.parametrize("chart_type", [ChartType.LINE, ChartType.AREA])
def test_epoch_millisecond_time_axis_keeps_utc_dates_and_real_spacing(monkeypatch, chart_type):
    figure = render_with_axes(monkeypatch, chart_type)
    assert mdates.num2date(figure["dates"][0]).isoformat() == "2026-01-01T00:00:00+00:00"
    assert mdates.num2date(figure["dates"][1]).isoformat() == "2026-01-01T00:05:00+00:00"
    assert mdates.num2date(figure["dates"][2]).isoformat() == "2026-01-01T00:15:00+00:00"
    assert any(":" in label for label in figure["xlabels"])
    assert "e12" not in figure["offset"]


@pytest.mark.parametrize("multiple", [False, True])
def test_heatmap_timestamp_labels_show_dates_instead_of_epoch_numbers(monkeypatch, multiple):
    figure = render_with_axes(monkeypatch, ChartType.HEATMAP, multiple=multiple)
    labels = figure["ylabels"] if multiple else figure["xlabels"]
    assert any("2026-01-01" in label and "00:05" in label for label in labels)
    assert not any("1767225" in label for label in labels)


def test_large_numeric_measure_is_not_mistaken_for_a_timestamp(monkeypatch):
    figure = render_with_axes(monkeypatch, ChartType.LINE, timestamp_type="LONG")
    assert figure["dates"] == [1767225600000, 1767225900000, 1767226500000]


def test_utc_axis_ignores_a_local_matplotlib_timezone_setting(monkeypatch):
    monkeypatch.setitem(matplotlib.rcParams, "timezone", "America/Los_Angeles")
    figure = render_with_axes(monkeypatch, ChartType.LINE)
    assert any("00:00" in label for label in figure["xlabels"])
    assert not any("16:00" in label for label in figure["xlabels"])


def test_short_window_has_multiple_readable_time_ticks(monkeypatch):
    figure = render_with_axes(monkeypatch, ChartType.LINE)
    assert len([label for label in figure["xlabels"] if ":" in label]) >= 3
