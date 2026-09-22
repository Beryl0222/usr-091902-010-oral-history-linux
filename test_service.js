"use strict";

const { spawnSync } = require("node:child_process");

// 运行全部 Python 测试：service_contract（健康入口）、test_domain（领域规则）、
// test_api（端到端 HTTP）。
const result = spawnSync(
  "python3",
  ["-m", "unittest", "-v", "service_contract", "test_domain", "test_api"],
  { stdio: "inherit" }
);
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
