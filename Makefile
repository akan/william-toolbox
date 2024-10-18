.PHONY: release
release: ## Build and package web assets	
	cd frontend && npm install && npm run build
	tar -czf web.static.tar.gz -C frontend/build .
	rm -rf src/williamtoolbox/web && mkdir -p src/williamtoolbox/web	
	mv web.static.tar.gz src/williamtoolbox/web/	
	cd src/williamtoolbox/web/ && tar -xzf web.static.tar.gz && rm web.static.tar.gz
	./deploy.sh && pip install -e .