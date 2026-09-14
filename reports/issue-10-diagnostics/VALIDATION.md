# Issue #10 本地验证记录

2026-09-14，Windows / Python 3.11.14。类别：运行诊断、打包工具、文档；
规则依据 R01/R08，策略、协议与结算规则不变。开发基线 aa6eb3e79c96e15e40ebc0c58817dd031ac6c933。

## 实际检查

- DSH 原生任务完成后，经 Codex 审核修正操控配对、版本归属及非有限数值诊断。
- Codex：`python -m unittest discover -s tests -v`，360 项通过，217.198 秒。
- Codex：`python -m unittest discover -s workflow -v`，59 项通过，6.275 秒。
- 诊断专项 25 项通过；已包含在上述 360 项内，不重复累计。
- `git diff --check` 与暂存区检查通过。官方原文、原始示例包、brain/protocol/simulator 未修改。
- 测试覆盖已提交基线包的真实解包/POST/GET、确定性构建、gzip 截断/CRC、清单篡改、
  路径安全；以及未知观测、实际攻击配对、日志故障不影响 HTTP、避免冒用父仓库版本。

以下哈希基于 Git 暂存区的规范化文件字节，可由包含本报告的提交复核。

```json
{
  "Demo/CoreGeek/main3.py": "13004ef4dc3f35e3feefe324fe7d99243cb682e33c32c0fab85402903d356256",
  "Demo/CoreGeek/src/agent/server.py": "ba408e73ca48050368bd0a861811b30c3dc3a0f49a515d0f433b383c0541aa1e",
  "Demo/CoreGeek/src/agent/diagnostics.py": "26d9d04815c96fcb19d69f464c57e655962ed0829f3f31e00e239737c531b178",
  "tools/build_submission.py": "fcb0262927de9c79073e0be73ba36fae5a3500248b52a3121bfcdbeb2a21be45",
  "tests/test_diagnostics.py": "2de06688736d72de477102d0c36fa53e1771c6792691177dda999db65b409b8c",
  "tests/test_diagnostics_review.py": "047c539652f89d2fbe19e8b2054555c22a0786e5d6727106e120d18df0d91936",
  "tests/test_build_submission.py": "30fe59c0489e01fd6c956f6b192ffb66949cf205534fa970195e8b40c4744d92"
}
```

## 最终包与验证边界

最终包必须在审核提交后由精确提交构建，禁止把上述旧基线打包测试冒充最终包验收。
最终候选的包 SHA256、两次构建一致性、解包后两个入口的 HTTP/版本身份验证，随 Release
资产 `codex-validation.json` 发布，并在 Issue #10 记录精确代码提交。
本报告不证明公司平台接收、Linux 启动或官方完整对局通过；没有内网 PASS。
