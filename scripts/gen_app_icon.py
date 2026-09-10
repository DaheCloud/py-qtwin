"""生成 assets/app.ico —— 与 ui/styles.py make_app_icon() 同款设计。

蓝色渐变圆角方块 + 白色文档（右上折角）+ 勾选线。
用 Pillow 绘制超采样大图后缩放，输出多尺寸 Windows 图标。

用法：
    .venv\\Scripts\\python.exe scripts\\gen_app_icon.py
如需更换图标，改下方颜色常量或替换绘制逻辑后重跑即可。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # 超采样画布，最后缩到 256 再存 ico

# 与 ui/styles.py 一致的主题色
ACCENT = (0x25, 0x63, 0xEB)        # 勾选线
ACCENT_HOVER = (0x3B, 0x82, 0xF6)  # 渐变起点（左上）
ACCENT_DEEP = (0x1D, 0x4E, 0xD8)   # 渐变终点（右下）
ACCENT_LIGHT = (0xDB, 0xEA, 0xFE)  # 文档折角
WHITE = (255, 255, 255)


def build() -> Image.Image:
    # 1) 对角线渐变：小图逐像素生成后放大（够平滑且快）
    grad = Image.new("RGB", (64, 64))
    px = grad.load()
    for y in range(64):
        for x in range(64):
            t = (x + y) / 126.0
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(ACCENT_HOVER, ACCENT_DEEP))
    bg = grad.resize((S, S), Image.Resampling.BICUBIC).convert("RGBA")

    # 2) 圆角矩形裁切
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=S * 0.22, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(img)

    # 3) 白色文档 + 右上折角（与 make_app_icon 几何一致）
    doc_l, doc_t, doc_r, doc_b = S * 0.26, S * 0.18, S * 0.74, S * 0.82
    fold = S * 0.16
    d.polygon(
        [(doc_l, doc_t), (doc_r - fold, doc_t), (doc_r, doc_t + fold), (doc_r, doc_b), (doc_l, doc_b)],
        fill=WHITE,
    )
    d.polygon(
        [(doc_r - fold, doc_t), (doc_r, doc_t + fold), (doc_r - fold, doc_t + fold)],
        fill=ACCENT_LIGHT,
    )

    # 4) 勾选线（圆头）
    width = int(S * 0.07)
    y_mid = doc_t + (doc_b - doc_t) * 0.52
    p1 = (doc_l + S * 0.08, y_mid)
    p2 = (doc_l + S * 0.20, y_mid + S * 0.10)
    p3 = (doc_r - S * 0.08, y_mid - S * 0.10)
    d.line([p1, p2, p3], fill=ACCENT, width=width, joint="curve")
    r = width / 2
    for cx, cy in (p1, p3):  # 手动补圆头端点
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)

    return img.resize((256, 256), Image.Resampling.LANCZOS)


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "assets" / "app.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    build().save(out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"[icon] OK -> {out}")


if __name__ == "__main__":
    main()
