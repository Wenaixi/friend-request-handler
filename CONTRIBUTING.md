# 贡献指南 (Contributing Guide)

欢迎参与本插件与 MaiBot 生态的开源建设！请在提交代码前遵循以下开发规范。

## 代码与文档规范

1. 语言：代码注释与面向用户的文档一律使用简体中文。
2. 风格：不使用 emoji 作为文档修饰或代码标记；避免无信息的空洞总结。
3. 类型约束：遵循 Python 3.10+ 类型标注（Type Hints），使用 Pydantic v2 声明配置模型。
4. 职责单一：保持模块高内聚低耦合，新功能或缺陷修复请提供完整的测试用例。

## 提交与分支流程

1. 基于 `main` 分支创建特性分支：`git checkout -b feature/your-feature-name`。
2. 保持小步提交，提交信息遵循 Conventional Commits 规范（如 `feat:`, `fix:`, `docs:`, `chore:`）。
3. 提交前确保所有单元测试通过。
4. 发起 Pull Request 并清晰描述改动背景、根因及验证结果。
