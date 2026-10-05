# 数智成果可信登记

这是一个用于记录数智成果可信登记领域规则的 Python 后端基础项目，当前提供不可变领域对象、稳定摘要和冲突检测等起点能力，运行时不依赖外部服务。

## 运行测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译检查

```bash
python3 -m compileall -q src tests run_cli.py
```

## 命令行示例

```bash
python3 run_cli.py
```
