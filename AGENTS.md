# AGENTS.md

给在本仓库工作的 AI 编程助手看的约定。人类读者请看 [README.md](README.md)。

## 提交信息规范

- 标题（subject）不超过 50 字符，首字母大写，用祈使句，结尾不加标点
- 标题与正文之间空一行；正文按 72 列换行
- 正文只在能提供有用信息时写，不要复述标题内容
- 只写标题就能说清时，不必硬凑正文

### AI 署名（必填）

由 AI 参与完成的提交，必须在正文末尾追加一行 git trailer，写明所用模型：

    AI-Model: <模型名称>

示例：

    AI-Model: DeepSeek V4.1 Flash

多个模型协作时写多行。

## 怎么写多行提交信息

不要用多个 `-m`（会变成多个段落，且不按 72 列换行）。用 `-F -` 从标准输入读：

    printf '%s\n' "Subject" "" "Body line 1" "Body line 2" \
      "AI-Model: DeepSeek V4.1 Flash" | git commit -F -

`printf` 的每个参数就是一行，空串 `""` 表示空行。
