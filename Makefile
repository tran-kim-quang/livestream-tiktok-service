.DEFAULT_GOAL := help

ifeq ($(OS),Windows_NT)
PYTHON ?= python
BRIDGE = powershell -NoProfile -ExecutionPolicy Bypass -File scripts/audio-bridge.ps1 $(BRIDGE_ARGS)
else
PYTHON ?= python3
BRIDGE = $(PYTHON) scripts/dev.py bridge $(BRIDGE_ARGS)
endif

.PHONY: help env build up wait bridge start watch stop restart logs status say stop-audio

help:
	@$(PYTHON) scripts/dev.py make-help

env:
	$(PYTHON) scripts/dev.py env $(if $(strip $(TIKTOK)),--tiktok "$(TIKTOK)",)

build:
	docker compose build

# Không build lại nếu image đã có (compose chỉ tự build khi chưa có image).
# Thêm BUILD=1 để build lại sau khi sửa code: make start BUILD=1
up: env
	docker compose up -d $(if $(strip $(BUILD)),--build,)

wait:
	$(PYTHON) scripts/dev.py wait

bridge:
	$(BRIDGE)

start:
	$(MAKE) up
	$(MAKE) wait
	$(MAKE) bridge

watch:
	$(if $(strip $(TIKTOK)),,$(error Thiếu TikTok username. Chạy: make watch TIKTOK=ten_kenh))
	$(PYTHON) scripts/dev.py env --tiktok "$(TIKTOK)"
	$(PYTHON) scripts/dev.py watch --tiktok "$(TIKTOK)"

stop:
	docker compose down

# Recreate thay vì restart để container đọc lại .env.
restart:
	docker compose up -d --force-recreate

logs:
	docker compose logs -f --tail=200

status:
	$(PYTHON) scripts/dev.py status

say:
	$(PYTHON) scripts/dev.py say $(if $(strip $(TEXT)),--text "$(TEXT)",)

stop-audio:
	$(PYTHON) scripts/dev.py stop-audio
