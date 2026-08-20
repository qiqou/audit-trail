# 脱敏样例数据

样本不随源代码提交；使用生成器按需创建，且生成器拒绝覆盖已有目录：

```bash
.venv/bin/python scripts/generate_testdata.py --size small --output /private/tmp/audit-trail-sample-small
```

规模为 `small`（3 单位 / 12 底稿 / 18 附件）、`medium`（20 / 200 / 400）和 `large`（100 / 5,000 / 20,000）。每个项目根目录生成 `sample_manifest.json`，记录 schema 版本与实际单位、底稿、附件、版本、交流、日志数量。

命名、正文、附件内容均为固定脱敏测试文本；不得导入任何真实客户资料。`large` 用于性能验证，应在专用临时目录创建并在测试完成后由执行人按本地保留政策清理。
