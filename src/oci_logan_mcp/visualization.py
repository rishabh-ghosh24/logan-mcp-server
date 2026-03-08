"""Visualization engine for generating charts from query results."""

import io
import base64
import logging
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime

import matplotlib

matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as ticker
import pandas as pd
import numpy as np
import seaborn as sns

logger = logging.getLogger(__name__)

# OCI Log Analytics / LA Explorer inspired color palette
LA_COLORS = [
    "#1f77b4",  # blue
    "#ff7f0e",  # orange
    "#2ca02c",  # green
    "#d62728",  # red
    "#9467bd",  # purple
    "#8c564b",  # brown
    "#e377c2",  # pink
    "#7f7f7f",  # gray
    "#bcbd22",  # olive
    "#17becf",  # cyan
    "#aec7e8",  # light blue
    "#ffbb78",  # light orange
    "#98df8a",  # light green
    "#ff9896",  # light red
    "#c5b0d5",  # light purple
]


class ChartType(Enum):
    """Supported chart types."""

    PIE = "pie"
    BAR = "bar"
    VERTICAL_BAR = "vertical_bar"
    LINE = "line"
    AREA = "area"
    TABLE = "table"
    TILE = "tile"
    TREEMAP = "treemap"
    HEATMAP = "heatmap"
    HISTOGRAM = "histogram"


class VisualizationEngine:
    """Generates visualizations from query results."""

    def __init__(self):
        """Initialize visualization engine."""
        self._setup_style()

    def _setup_style(self) -> None:
        """Configure matplotlib style."""
        try:
            plt.style.use("seaborn-v0_8-whitegrid")
        except OSError:
            plt.style.use("default")
        plt.rcParams["figure.figsize"] = (12, 6)
        plt.rcParams["figure.dpi"] = 100
        plt.rcParams["axes.prop_cycle"] = plt.cycler(color=LA_COLORS)

    def generate(
        self,
        data: Dict[str, Any],
        chart_type: ChartType,
        title: Optional[str] = None,
        options: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Generate visualization and return image + data."""
        options = options or {}
        df = self._to_dataframe(data)

        if df.empty:
            return self._generate_empty_chart(title)

        chart_generators = {
            ChartType.PIE: self._generate_pie,
            ChartType.BAR: self._generate_bar,
            ChartType.VERTICAL_BAR: self._generate_vertical_bar,
            ChartType.LINE: self._generate_line,
            ChartType.AREA: self._generate_area,
            ChartType.TABLE: self._generate_table,
            ChartType.TILE: self._generate_tile,
            ChartType.TREEMAP: self._generate_treemap,
            ChartType.HEATMAP: self._generate_heatmap,
            ChartType.HISTOGRAM: self._generate_histogram,
        }

        generator = chart_generators.get(chart_type)
        if not generator:
            raise ValueError(f"Unsupported chart type: {chart_type}")

        image = generator(df, title, options)

        return {
            "image_base64": image,
            "image_format": "png",
            "raw_data": df.to_dict(orient="records"),
            "chart_type": chart_type.value,
        }

    def _to_dataframe(self, data: Dict) -> pd.DataFrame:
        """Convert query result to DataFrame."""
        columns = [
            col.get("name", f"col_{i}") for i, col in enumerate(data.get("columns", []))
        ]
        rows = data.get("rows", [])

        materialized_rows = []
        for row in rows:
            if row is None:
                continue
            elif callable(row):
                materialized_rows.append(list(row()))
            elif hasattr(row, '__iter__') and not isinstance(row, (str, dict)):
                materialized_rows.append(list(row))
            elif isinstance(row, dict):
                materialized_rows.append(list(row.values()))
            else:
                materialized_rows.append([row])

        if not materialized_rows:
            return pd.DataFrame()

        if not columns:
            first_row = materialized_rows[0] if materialized_rows else []
            columns = [f"col_{i}" for i in range(len(first_row))]

        num_cols = len(columns)
        normalized_rows = []
        for row in materialized_rows:
            if len(row) < num_cols:
                row = list(row) + [None] * (num_cols - len(row))
            elif len(row) > num_cols:
                row = list(row)[:num_cols]
            normalized_rows.append(row)

        try:
            return pd.DataFrame(normalized_rows, columns=columns)
        except Exception as e:
            logger.error(f"Error creating DataFrame: {e}")
            return pd.DataFrame()

    def _is_datetime_column(self, series: pd.Series) -> bool:
        """Check if a series contains datetime-like values."""
        if series.dtype == "datetime64[ns]":
            return True

        # Sample first few non-null values
        sample = series.dropna().head(5)
        if sample.empty:
            return False

        parsed = 0
        for val in sample:
            try:
                s = str(val).strip()
                # Common LA datetime formats
                pd.to_datetime(s)
                parsed += 1
            except (ValueError, TypeError):
                pass

        return parsed >= len(sample) * 0.8

    def _parse_datetime_column(self, series: pd.Series) -> pd.Series:
        """Parse a series into datetime values."""
        try:
            return pd.to_datetime(series, utc=True)
        except Exception:
            try:
                return pd.to_datetime(series)
            except Exception:
                return series

    def _format_datetime_axis(self, ax: plt.Axes, dates: pd.Series) -> None:
        """Auto-format datetime x-axis based on date range."""
        if dates.empty:
            return

        try:
            date_range = dates.max() - dates.min()
            total_hours = date_range.total_seconds() / 3600

            if total_hours <= 2:
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
                ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=15))
            elif total_hours <= 24:
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
                ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
            elif total_hours <= 168:  # 7 days
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M"))
                ax.xaxis.set_major_locator(mdates.DayLocator())
            elif total_hours <= 720:  # 30 days
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
                ax.xaxis.set_major_locator(mdates.DayLocator(interval=3))
            else:
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator())

            plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
        except Exception as e:
            logger.debug(f"Could not format datetime axis: {e}")
            plt.xticks(rotation=45, ha="right")

    def _format_count_axis(self, ax: plt.Axes, axis: str = "y") -> None:
        """Format count axis with K/M suffixes for large numbers."""
        def format_func(value, pos):
            if value >= 1_000_000:
                return f"{value / 1_000_000:.1f}M"
            elif value >= 1_000:
                return f"{value / 1_000:.0f}K"
            else:
                return f"{int(value)}"

        if axis == "y":
            ax.yaxis.set_major_formatter(ticker.FuncFormatter(format_func))
        else:
            ax.xaxis.set_major_formatter(ticker.FuncFormatter(format_func))

    def _generate_empty_chart(self, title: Optional[str]) -> Dict[str, Any]:
        """Generate an empty chart placeholder."""
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(
            0.5, 0.5, "No data available",
            ha="center", va="center", fontsize=16, color="gray",
        )
        ax.axis("off")
        if title:
            ax.set_title(title)

        return {
            "image_base64": self._fig_to_base64(fig),
            "image_format": "png",
            "raw_data": [],
            "chart_type": "empty",
        }

    def _generate_pie(self, df: pd.DataFrame, title: Optional[str], options: Dict) -> str:
        """Generate pie chart."""
        fig, ax = plt.subplots()

        label_col = df.columns[0]
        value_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]

        values = pd.to_numeric(df[value_col], errors="coerce").fillna(0)

        # Limit slices to top 10 + "Other" for readability
        if len(values) > 10:
            top_idx = values.nlargest(10).index
            other_sum = values.drop(top_idx).sum()
            values = pd.concat([values.loc[top_idx], pd.Series([other_sum])])
            labels = list(df[label_col].iloc[top_idx]) + ["Other"]
        else:
            labels = df[label_col].tolist()

        colors = LA_COLORS[: len(values)]
        ax.pie(
            values,
            labels=labels,
            autopct="%1.1f%%",
            startangle=90,
            colors=colors,
        )
        ax.set_title(title or f"Distribution by {label_col}")

        return self._fig_to_base64(fig)

    def _generate_bar(self, df: pd.DataFrame, title: Optional[str], options: Dict) -> str:
        """Generate bar chart.

        Supports stacked bars when data has 3+ columns
        (first col = labels, remaining cols = value series).
        """
        fig, ax = plt.subplots()

        label_col = df.columns[0]
        value_cols = [c for c in df.columns[1:] if c != label_col]

        if not value_cols:
            value_cols = [label_col]

        labels = df[label_col].astype(str)

        # Check if we have multiple value columns (stacked bar)
        if len(value_cols) > 1:
            # Stacked horizontal bar chart
            left = np.zeros(len(labels))
            for i, col in enumerate(value_cols):
                values = pd.to_numeric(df[col], errors="coerce").fillna(0)
                color = LA_COLORS[i % len(LA_COLORS)]
                ax.barh(labels, values, left=left, label=col, color=color)
                left += values.values

            ax.legend(
                loc="upper right",
                fontsize=9,
                framealpha=0.9,
            )
        else:
            # Simple bar chart
            values = pd.to_numeric(df[value_cols[0]], errors="coerce").fillna(0)
            colors = LA_COLORS[: len(labels)]
            ax.barh(labels, values, color=colors)

        ax.set_xlabel(value_cols[0] if len(value_cols) == 1 else "Count")
        ax.set_ylabel(label_col)
        ax.set_title(title or f"{'|'.join(value_cols)} by {label_col}")
        self._format_count_axis(ax, axis="x")

        plt.tight_layout()
        return self._fig_to_base64(fig)

    def _generate_line(self, df: pd.DataFrame, title: Optional[str], options: Dict) -> str:
        """Generate line chart with proper datetime axis support.

        Handles timestats output from LA queries where the first column
        is a datetime and subsequent columns are value series.
        """
        fig, ax = plt.subplots()

        x_col = df.columns[0]
        value_cols = list(df.columns[1:])

        # Try to parse x-axis as datetime
        is_datetime = self._is_datetime_column(df[x_col])

        if is_datetime:
            x_values = self._parse_datetime_column(df[x_col])
            # Sort by time
            sort_idx = x_values.argsort()
            x_values = x_values.iloc[sort_idx]
            df = df.iloc[sort_idx]
        else:
            x_values = df[x_col]

        # Plot each value column as a separate line
        for i, col in enumerate(value_cols):
            values = pd.to_numeric(df[col], errors="coerce").fillna(0)
            color = LA_COLORS[i % len(LA_COLORS)]
            ax.plot(
                x_values,
                values,
                label=col,
                marker="o" if len(df) <= 30 else None,
                markersize=4,
                linewidth=2,
                color=color,
            )

        # Format axes
        if is_datetime:
            self._format_datetime_axis(ax, x_values)
        else:
            plt.xticks(rotation=45, ha="right")

        self._format_count_axis(ax, axis="y")

        ax.set_xlabel(x_col if not is_datetime else "")
        ax.set_ylabel(value_cols[0] if len(value_cols) == 1 else "Count")
        ax.set_title(title or "Trend Over Time")

        if len(value_cols) > 1:
            ax.legend(
                loc="upper left",
                fontsize=9,
                framealpha=0.9,
            )

        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        return self._fig_to_base64(fig)

    def _generate_area(self, df: pd.DataFrame, title: Optional[str], options: Dict) -> str:
        """Generate area chart with proper datetime axis and multi-series support.

        Supports stacked area for multi-series (timestats with by clause).
        """
        fig, ax = plt.subplots()

        x_col = df.columns[0]
        value_cols = list(df.columns[1:])

        # Try to parse x-axis as datetime
        is_datetime = self._is_datetime_column(df[x_col])

        if is_datetime:
            x_values = self._parse_datetime_column(df[x_col])
            sort_idx = x_values.argsort()
            x_values = x_values.iloc[sort_idx]
            df = df.iloc[sort_idx]
        else:
            x_values = df[x_col]

        if len(value_cols) > 1:
            # Stacked area chart
            y_arrays = []
            labels = []
            for col in value_cols:
                values = pd.to_numeric(df[col], errors="coerce").fillna(0)
                y_arrays.append(values.values)
                labels.append(col)

            colors = LA_COLORS[: len(y_arrays)]
            ax.stackplot(
                x_values, *y_arrays,
                labels=labels,
                colors=colors,
                alpha=0.7,
            )
            ax.legend(
                loc="upper left",
                fontsize=9,
                framealpha=0.9,
            )
        else:
            # Single area
            values = pd.to_numeric(df[value_cols[0]], errors="coerce").fillna(0)
            ax.fill_between(x_values, values, alpha=0.4, color=LA_COLORS[0])
            ax.plot(x_values, values, linewidth=2, color=LA_COLORS[0])

        # Format axes
        if is_datetime:
            self._format_datetime_axis(ax, x_values)
        else:
            plt.xticks(rotation=45, ha="right")

        self._format_count_axis(ax, axis="y")

        ax.set_xlabel(x_col if not is_datetime else "")
        ax.set_ylabel(value_cols[0] if len(value_cols) == 1 else "Count")
        ax.set_title(title or "Volume Over Time")
        ax.grid(True, alpha=0.3)

        plt.tight_layout()
        return self._fig_to_base64(fig)

    def _generate_table(self, df: pd.DataFrame, title: Optional[str], options: Dict) -> str:
        """Generate table visualization."""
        display_df = df.head(20)

        fig, ax = plt.subplots(figsize=(12, len(display_df) * 0.5 + 2))
        ax.axis("off")

        table = ax.table(
            cellText=display_df.values,
            colLabels=display_df.columns,
            cellLoc="center",
            loc="center",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1.2, 1.5)

        # Style header row
        for j in range(len(display_df.columns)):
            table[0, j].set_facecolor(LA_COLORS[0])
            table[0, j].set_text_props(color="white", fontweight="bold")

        if title:
            ax.set_title(title, pad=20)

        plt.tight_layout()
        return self._fig_to_base64(fig)

    def _generate_tile(self, df: pd.DataFrame, title: Optional[str], options: Dict) -> str:
        """Generate single-value tile."""
        fig, ax = plt.subplots(figsize=(4, 3))
        ax.axis("off")

        value = "N/A"
        if len(df) > 0:
            if len(df.columns) > 1:
                value = df.iloc[0, -1]
            else:
                value = df.iloc[0, 0]

        if isinstance(value, (int, float)):
            if value >= 1_000_000:
                display_value = f"{value/1_000_000:.1f}M"
            elif value >= 1_000:
                display_value = f"{value/1_000:.1f}K"
            else:
                display_value = f"{value:,.0f}" if isinstance(value, float) else f"{value:,}"
        else:
            display_value = str(value)

        ax.text(
            0.5, 0.5, display_value,
            ha="center", va="center", fontsize=48, fontweight="bold",
            color=LA_COLORS[0],
        )

        if title:
            ax.text(0.5, 0.15, title, ha="center", va="center", fontsize=14, color="#555")

        return self._fig_to_base64(fig)

    def _generate_treemap(self, df: pd.DataFrame, title: Optional[str], options: Dict) -> str:
        """Generate treemap visualization.

        Uses squarify if available, falls back to horizontal bar.
        """
        try:
            import squarify
            return self._generate_treemap_squarify(df, title, options, squarify)
        except ImportError:
            # Fall back to bar chart
            return self._generate_bar(df, title or "Treemap (install squarify for true treemap)", options)

    def _generate_treemap_squarify(
        self, df: pd.DataFrame, title: Optional[str], options: Dict, squarify
    ) -> str:
        """Generate treemap using squarify library."""
        fig, ax = plt.subplots()

        label_col = df.columns[0]
        value_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]

        values = pd.to_numeric(df[value_col], errors="coerce").fillna(0)
        labels = df[label_col].astype(str)

        # Top 15 for readability
        if len(values) > 15:
            top_idx = values.nlargest(15).index
            values = values.loc[top_idx]
            labels = labels.loc[top_idx]

        colors = LA_COLORS[: len(values)]
        squarify.plot(
            sizes=values.values,
            label=[f"{l}\n{v:,.0f}" for l, v in zip(labels, values)],
            color=colors,
            alpha=0.8,
            ax=ax,
        )
        ax.set_title(title or f"Distribution by {label_col}")
        ax.axis("off")

        return self._fig_to_base64(fig)

    def _generate_vertical_bar(self, df: pd.DataFrame, title: Optional[str], options: Dict) -> str:
        """Generate vertical bar chart.

        Standard vertical bar chart (LA Explorer's default "Bar" type).
        Supports stacked bars when data has 3+ columns.
        """
        fig, ax = plt.subplots()

        label_col = df.columns[0]
        value_cols = [c for c in df.columns[1:] if c != label_col]

        if not value_cols:
            value_cols = [label_col]

        labels = df[label_col].astype(str)

        if len(value_cols) > 1:
            # Stacked vertical bar chart
            bottom = np.zeros(len(labels))
            for i, col in enumerate(value_cols):
                values = pd.to_numeric(df[col], errors="coerce").fillna(0)
                color = LA_COLORS[i % len(LA_COLORS)]
                ax.bar(labels, values, bottom=bottom, label=col, color=color)
                bottom += values.values

            ax.legend(
                loc="upper right",
                fontsize=9,
                framealpha=0.9,
            )
        else:
            # Simple vertical bar chart
            values = pd.to_numeric(df[value_cols[0]], errors="coerce").fillna(0)
            colors = LA_COLORS[: len(labels)]
            ax.bar(labels, values, color=colors)

        ax.set_xlabel(label_col)
        ax.set_ylabel(value_cols[0] if len(value_cols) == 1 else "Count")
        ax.set_title(title or f"{'|'.join(value_cols)} by {label_col}")
        self._format_count_axis(ax, axis="y")

        # Rotate labels if many categories
        if len(labels) > 6:
            plt.xticks(rotation=45, ha="right")

        plt.tight_layout()
        return self._fig_to_base64(fig)

    def _generate_heatmap(self, df: pd.DataFrame, title: Optional[str], options: Dict) -> str:
        """Generate heatmap visualization.

        Creates a color-coded matrix for time x category patterns.
        Best for queries like: timestats count by 'Log Source' span=1h
        """
        fig, ax = plt.subplots(figsize=(14, 8))

        x_col = df.columns[0]
        value_cols = list(df.columns[1:])

        if len(value_cols) >= 2:
            # Multi-column data: first col = rows, remaining cols = value series
            # Create a pivot-like matrix
            row_labels = df[x_col].astype(str)
            matrix_data = df[value_cols].apply(pd.to_numeric, errors="coerce").fillna(0)
            matrix_data.index = row_labels

            sns.heatmap(
                matrix_data,
                annot=len(matrix_data) <= 15 and len(value_cols) <= 10,
                fmt=".0f",
                cmap="YlOrRd",
                linewidths=0.5,
                linecolor="white",
                ax=ax,
                cbar_kws={"label": "Count"},
            )
            ax.set_ylabel(x_col)
        else:
            # Single value column — try to detect datetime for a time-based heatmap
            # Fall back to showing single-column as a 1D heatmap
            values = pd.to_numeric(df[value_cols[0]], errors="coerce").fillna(0)
            row_labels = df[x_col].astype(str)

            matrix = values.values.reshape(1, -1)
            sns.heatmap(
                pd.DataFrame(matrix, columns=row_labels, index=[value_cols[0]]),
                annot=len(row_labels) <= 20,
                fmt=".0f",
                cmap="YlOrRd",
                linewidths=0.5,
                linecolor="white",
                ax=ax,
                cbar_kws={"label": "Count"},
            )

        ax.set_title(title or "Heatmap")
        plt.tight_layout()
        return self._fig_to_base64(fig)

    def _generate_histogram(self, df: pd.DataFrame, title: Optional[str], options: Dict) -> str:
        """Generate histogram for distribution of numeric values.

        Useful for queries like: * | stats count by 'Response Time'
        Shows the distribution pattern of the data.
        """
        fig, ax = plt.subplots()

        # Use the last numeric column as the value column
        value_col = None
        for col in reversed(df.columns):
            numeric = pd.to_numeric(df[col], errors="coerce")
            if numeric.notna().sum() > 0:
                value_col = col
                break

        if value_col is None:
            value_col = df.columns[-1]

        values = pd.to_numeric(df[value_col], errors="coerce").dropna()

        if values.empty:
            return self._generate_empty_chart(title)["image_base64"]

        # Auto-select number of bins
        n = len(values)
        if n <= 10:
            bins = n
        elif n <= 50:
            bins = 15
        else:
            bins = min(50, int(np.sqrt(n)))

        ax.hist(
            values,
            bins=bins,
            color=LA_COLORS[0],
            edgecolor="white",
            alpha=0.85,
        )

        # Add mean line
        mean_val = values.mean()
        ax.axvline(
            mean_val, color=LA_COLORS[3], linestyle="--",
            linewidth=2, label=f"Mean: {mean_val:,.1f}",
        )

        ax.set_xlabel(value_col)
        ax.set_ylabel("Frequency")
        ax.set_title(title or f"Distribution of {value_col}")
        ax.legend(loc="upper right", fontsize=9)
        self._format_count_axis(ax, axis="y")
        ax.grid(True, alpha=0.3, axis="y")

        plt.tight_layout()
        return self._fig_to_base64(fig)

    def _fig_to_base64(self, fig) -> str:
        """Convert matplotlib figure to base64 PNG."""
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
