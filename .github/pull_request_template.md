<!-- 中文或英文均可。指南：../CONTRIBUTING.md（仓库根目录）。
Chinese and English are welcome. See CONTRIBUTING.en.md in the repository root.
删除不适用的提示。/ Remove prompts that do not apply. -->

## 问题与改动 / Problem and Changes

<!-- 说明具体问题及改动后的行为。/ Explain the problem and resulting behavior. -->

## 关联 Issue / Related Issues

<!-- 没有关联 Issue 时填写“无 / None”。/ Write "None" if there is no related issue. -->

## 验证 / Verification

<!-- 列出实际运行的命令与结果，以及未执行的验证和原因。
List commands actually run and their results, plus checks not performed and why.
自动测试、构建、真实 Provider、安装版 Desktop、生产 SSH 请分别说明。
Distinguish automated tests, builds, real providers, installed Desktop, and production SSH. -->

## 截图与兼容性影响（如适用）/ Screenshots and Compatibility Impact (If Applicable)

<!-- UI 改动附脱敏截图；持久化或兼容性变更说明升级影响。
Include sanitized screenshots for UI changes and upgrade implications for persistence or compatibility changes. -->

## 提交检查 / Checklist

- [ ] 改动聚焦且不包含凭据、运行时数据或构建产物 / Changes are focused and contain no credentials, runtime data, or build outputs.
- [ ] 已检查相关 AGENTS.md；需要时同步领域文档 / Relevant AGENTS.md rules reviewed and domain documentation updated where needed.
- [ ] 行为变更已补充测试，或在验证部分解释原因 / Behavior changes have tests, or the verification section explains why not.
- [ ] 已运行 `git diff --check` / Ran `git diff --check`.
