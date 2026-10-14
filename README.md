# LAAD3 for macOS Dictionary.app

把《朗文高阶美语词典》第 3 版（Longman Advanced American Dictionary, LAAD3）的 MDict 文件（.mdx/.mdd）
转换成 macOS 自带"词典"应用可以直接使用的 `LAAD3.dictionary` 包。

## 仓库内容

- `tools/build_laad3.py` 转换脚本：MDX 记录 → Dictionary Development Kit 的 XML 工程（含 Makefile、plist、CSS）
- `tools/mdict_lib.py` 无第三方依赖的 MDict 读取库（.mdx/.mdd，1.2/2.0 引擎）
- `tools/assets/` 样式表（浅色/深色）、显示偏好 XSL、偏好面板、喇叭图标

MDX/MDD 原始文件、生成的 `LAAD3.dictionary` 和 zip 都不在仓库里（见 `.gitignore`）。

## 直接使用现成的词典包

1. 下载 `LAAD3.dictionary.zip` 并解压。
2. 把 `LAAD3.dictionary` 放进 `~/Library/Dictionaries/`（没有就新建）。
3. 重启"词典"应用，在 词典 › 设置 里勾选 Longman Advanced American Dictionary。

## 自己构建

需要：

- macOS 及 Dictionary Development Kit（在 Apple 的 "Additional Tools for Xcode" 里，放到
  `/Applications/Utilities/Dictionary Development Kit`）
- Python 3
- LAAD3 的 MDict 文件：`LongmanAdvancedAmericanDictionary3thEnEn.mdx` 和 `.mdd`，放在同一目录

```sh
python3 -I tools/build_laad3.py <mdx所在目录> project   # 生成 project/LAAD3.xml 等，并从 MDD 解出音频/图片
cd project && make && make install                      # 构建并安装到 ~/Library/Dictionaries
```

然后重启"词典"应用。调样式时可用 `--limit N` 只转换前 N 条；`--skip-resources` 跳过重新解压音频/图片。

## 功能

- 词源 / 动词表 / 搭配 / 同义词 按钮在词条内展开。按钮的 `<summary>` 带有 `apple_mouseover_disable="1"`，
  否则"词典"应用注入的点击脚本会把点击当成查词（跳到 etymology 等词条）。
- 喇叭图标播放单词和例句的美音。
- 屈折形式、短语动词、派生词、拼写变体、习语都能直接查到，也支持强制点按 / 三指轻点取词。
- 跟随系统的浅色 / 深色外观；词典 › 设置 里可开关例句发音、图片和同义词/搭配展开。

## 版权

词典内容版权归 Pearson Education 所有，仅供个人学习使用。
