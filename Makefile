# =============================================================================
# 助贷 AI 中台 —— 常用命令
# 用法： make <target>
# =============================================================================
PYTHON ?= python
PYTHONPATH := src
export PYTHONPATH

.DEFAULT_GOAL := help

.PHONY: help
help: ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## 安装运行时依赖
	$(PYTHON) -m pip install -r requirements.txt

.PHONY: install-dev
install-dev: ## 安装开发+测试依赖
	$(PYTHON) -m pip install -r requirements-dev.txt

.PHONY: seed
seed: ## 灌入演示数据（产品政策 / 客户画像）
	$(PYTHON) scripts/seed_data.py

.PHONY: test
test: ## 运行全部测试
	$(PYTHON) -m pytest

.PHONY: test-cov
test-cov: ## 运行测试并输出覆盖率
	$(PYTHON) -m pytest --cov=src --cov-report=term-missing

.PHONY: lint
lint: ## 代码规范检查
	$(PYTHON) -m ruff check src tests

.PHONY: fmt
fmt: ## 自动修复代码规范问题
	$(PYTHON) -m ruff check --fix src tests

.PHONY: run
run: seed ## 启动 API 服务（先灌数据）
	$(PYTHON) -m uvicorn --app-dir src agent_platform.api.app:app --host 0.0.0.0 --port 8000 --reload

.PHONY: demo
demo: ## 端到端演示：跑通四大服务的核心链路
	$(PYTHON) scripts/demo.py

.PHONY: web-install
web-install: ## 安装前端依赖（web/，首次或 package.json 变更后执行）
	cd web && npm install

.PHONY: web-dev
web-dev: ## 启动前端 dev server（默认 5173，API 代理到 8000）
	cd web && npm run dev

.PHONY: web-build
web-build: ## 构建前端产物到 web/dist（由 FastAPI 挂在 /ui 下）
	cd web && npm run build

.PHONY: docker-build
docker-build: ## 构建镜像
	docker build -t zhudai-ai-suite:1.0.0 .

.PHONY: docker-up
docker-up: ## 用 docker compose 启动全套（含 postgres / qdrant / redis）
	docker compose up -d --build

.PHONY: docker-down
docker-down: ## 停止 docker compose
	docker compose down
