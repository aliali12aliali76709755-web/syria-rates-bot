"""Matplotlib chart generation (Agg / no display). Titles use Latin text to avoid
Arabic shaping issues inside matplotlib."""
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


def build_chart_png(points, code, days):
    """points: list of (datetime, value). Returns BytesIO PNG."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]

    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=120)
    fig.patch.set_facecolor("#0f1115")
    ax.set_facecolor("#0f1115")

    line_color = "#e8b94a"
    ax.plot(xs, ys, color=line_color, linewidth=2.4, marker="o",
            markersize=3.5, markerfacecolor=line_color)
    ax.fill_between(xs, ys, min(ys), color=line_color, alpha=0.12)

    import matplotlib.ticker as ticker
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, pos: f"{x:,.2f}" if x < 1000 else f"{x:,.0f}"))

    ax.set_title(f"{code} / SYP  —  last {days} days", color="#f5f5f5",
                 fontsize=14, pad=14, fontweight="bold")
    ax.tick_params(colors="#9aa0a6", labelsize=9)
    for spine in ax.spines.values():
        spine.set_color("#2a2f3a")
    ax.grid(True, color="#232833", linewidth=0.8, alpha=0.7)

    if days <= 2:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.autofmt_xdate(rotation=30)

    last = ys[-1]
    first = ys[0]
    change = last - first
    pct = (change / first * 100) if first else 0
    sign = "+" if change >= 0 else ""
    ax.annotate(f"{last:,.2f}  ({sign}{pct:.2f}%)",
                xy=(xs[-1], last), xytext=(8, 8), textcoords="offset points",
                color=("#4caf50" if change >= 0 else "#ef5350"),
                fontsize=11, fontweight="bold")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf
