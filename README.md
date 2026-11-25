# PlemolNoHan

**PlemolNoHan** is a reproducible subset build of [yuru7/PlemolJP](https://github.com/yuru7/PlemolJP) (IBM Plex Mono + IBM Plex Sans JP) that strips Han ideographs, CJK fullwidth punctuation, and double-width `EAW=N` symbols so Simplified Chinese users can pair PlemolJP's tuned coding Latin metrics with any preferred Chinese font without glyph overlap or table misalignment.

---

**PlemolNoHan** 是基于开源编程字体 [yuru7/PlemolJP](https://github.com/yuru7/PlemolJP) 的可复现裁剪版：在完整保留其英文字形、字号比例、连字与 OpenType 样式集（如斜杠 `0`、单层 `a`/`g` 等）的前提下，移除了汉字、全角中文标点以及“Unicode 窄字符（`EAW=N`）却被做成 2 格宽”的残留符号。

## 为什么需要 PlemolNoHan？

1. **自由搭配中文字体**：原版 PlemolJP 内置大量日文汉字（IBM Plex Sans JP），在终端或编辑器中会拦截中文字符渲染，导致简中文本呈现日文异体字形或字体风格割裂。剔除汉字与全角中文标点后，中文可以稳定回退到冬青黑体（Hiragino Sans GB）、苹方（PingFang SC）、思源黑体等。
2. **修复终端符号重叠、大小不一与 Emacs 表格错位**：原版 PlemolJP 中有一批 Unicode East Asian Width 为 `N`（明确窄字符，应占 1 格）的字符被补成了 2 格宽（`1056` 单位，如 `⌘ U+2318`、`⏎ U+23CE`、`⚠ U+26A0`、`♻ U+267B`、`☀`、`✂`、`ϕ` 与 IPA 音标 `ɛ ɜ ˈ ˌ` 等；Console 系列含 130 个，Standard 系列含 244 个）：
   - 在 **Ghostty** 等按 Unicode 标准分配 1 格宽度的终端中，这些符号向右溢出撞上紧邻字符（例如 `⌘D`、`⌘⌥` 重叠）。
   - 在 **GUI Emacs**（尤其是 Org-mode 表格）中，Emacs 按 1 列计算宽度但字形实际推进 2 列，导致表格整行错位。
3. **内置等比例、留白充足的 1 格宽 macOS 按键图标**：从同为 SIL OFL 1.1 协议的 [Paper Mono](https://github.com/paper-design/paper-mono) 提取 `⌘ ⌥ ⇧ ⌃ ⌫ ⎋ ⏎` 7 个按键字形，按 8 档字重动态补偿笔画粗细，并统一按 `PlemolNoHan` 的 `O` 水平框与 CapHeight 中线居中缩放，**无需**在 Ghostty 或 Emacs 里额外配置 `font-codepoint-map`，即可与英文字母（`⌘D`、`⌘W`、`⌘⇧Enter`）保持均衡留白与高度一致。

## 裁剪与补全规则

默认构建针对上游 `PlemolJP` 全套 64 个字重（`PlemolNoHan`、`PlemolNoHan Console`、`PlemolNoHan35`、`PlemolNoHan35 Console`）执行以下规则：

| 类别 | Unicode 范围 / 判定条件 | 处理方式 | 说明 |
| --- | --- | --- | --- |
| 汉字与部首 | `U+2E80–2EFF`, `U+2F00–2FDF`, `U+3400–4DBF`, `U+4E00–9FFF`, `U+F900–FAFF`, `U+20000–3FFFF` | **移除** | 释放给终端/编辑器的中文字体回退链 |
| CJK 标点与全角 ASCII | `U+3001–303F`, `U+FF01–FF5E` | **移除** | 让 `、。「」！？` 等也回退到中文字体（**保留** `U+3000` 全角空格与半角片假名 `U+FF61–FF9F`） |
| 错宽窄符号 (`EAW=N`) | 非 PUA 区间内 `unicodedata.east_asian_width(ch) == "N"` 且 `advance > 1.1 × D_adv` | **移除** | 移除 `⌘`、`⏎`、`⚠`、`✂`、`ϕ`、IPA 音标等错宽残留字形（Console 版 130 个，Standard 版 244 个） |
| macOS 按键图标 | `⇧` `⌃` `⌘` `⌥` `⌫` `⎋` `⏎` (`U+21E7,2303,2318,2325,232B,238B,23CE`) | **内置重绘** | 使用 Paper Mono 矢量化缩放 + 笔画字重补偿嵌入单格（可用 `--no-keycap-patch` 关闭） |
| 日文假名 | `U+3040–30FF`, `U+31F0–31FF`, `U+FF61–FF9F` | **默认保留** | 可通过 `--strip-kana` 一并裁掉 |
| 模糊宽度符号 (`EAW=A`) | `U+2000–2BFF` 中 `EAW=A` 的 2 格符号（如 `①–⑳`、`❶–❿`、`Ⓐ–Ⓩ`） | **默认保留** | 可通过 `--strip-ambiguous-wide` 一并裁掉 |
| OpenType 特性与样式集 | `layout_features=["*"]`, `name_IDs=["*"]` | **完整保留** | 保留 `zero`、`ss01`–`ss09` 等全部特性与 UI 名称 |

## 配置示例

### Ghostty (`~/.config/ghostty/config`)

```ini
font-family = "PlemolNoHan Console"
font-family = "Hiragino Sans GB"
font-size = 14

# 如需 Nerd Font 图标，可搭配上游 PlemolJP Console NF 映射 PUA 区间
font-codepoint-map = U+E000-U+F8FF,U+F0000-U+FFFFD=PlemolJP Console NF
```

### Emacs (`init.el`)

```elisp
(when (display-graphic-p)
  (let* ((font-size 14)
         ;; PlemolNoHan Console 半角宽为 0.528em；按像素列宽严格 2:1 缩放冬青黑体
         (column (round (* 0.528 font-size)))
         (cjk-ratio (/ (+ (* 2 column) 0.5) font-size)))
    (setf (alist-get "Hiragino Sans GB" face-font-rescale-alist nil nil #'equal) cjk-ratio)
    (set-face-attribute 'default nil :font (font-spec :family "PlemolNoHan Console" :size font-size))
    (set-fontset-font t 'han (font-spec :family "Hiragino Sans GB"))
    (set-fontset-font t 'cjk-misc (font-spec :family "Hiragino Sans GB"))))
```

## 本地构建与验证

依赖 [uv](https://docs.astral.sh/uv/) 与 Python >= 3.11：

```bash
# 1. 安装依赖
uv sync

# 2. 自动下载 PlemolJP v3.1.0 与 Paper Mono v1.000，裁剪并嵌入按键图标到 dist/
uv run python scripts/build.py --version 3.1.0

#    macOS 可追加 --install-macos 直接安装到 ~/Library/Fonts/
uv run python scripts/build.py --version 3.1.0 --install-macos

# 3. 校验字体表、字符覆盖率、按键图标留白/对齐与 OFL 合规性
uv run python scripts/verify.py --dir dist --expected-count 64
```

常用可选参数：
- `--variant {standard,nf,hs}`：构建普通版、`NF` 版或 `HS`（显示全角空格）版
- `--strip-kana`：同时移除日文平假名/片假名
- `--strip-ambiguous-wide`：同时移除 `U+2000–2BFF` 内 `EAW=A` 的 2 格符号
- `--no-keycap-patch`：关闭从 Paper Mono 嵌入单格按键图标

## 致谢与开源协议

- 上游字体：[yuru7/PlemolJP](https://github.com/yuru7/PlemolJP)（基于 [IBM Plex](https://github.com/IBM/plex)、[Hack](https://github.com/source-foundry/Hack) 与 [Nerd Fonts](https://github.com/ryanoasis/nerd-fonts)）
- macOS 按键图标来源：[paper-design/paper-mono](https://github.com/paper-design/paper-mono)（SIL OFL 1.1）
- 字体本身遵循 **SIL Open Font License 1.1**（`Plex` 与 `PlemolJP` 为各自版权方的 Reserved Font Name，本衍生项目已依约改名为 `PlemolNoHan` 并完整保留上游版权声明）。
- 本仓库内的构建与校验脚本遵循 **MIT License**，详见 [LICENSE](LICENSE)。
