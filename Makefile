.PHONY: install run predict
install:
	python -m pip install -r backend/requirements.txt
run:
	cd backend && uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
predict:
	cd backend && PYTHONPATH=. python -m app.predict --input "$(abspath $(IN))" --output "$(abspath $(OUT))"

.PHONY: test
test:
	cd backend && PYTHONPATH=. python -m unittest discover -s ../tests -v

.PHONY: annotate evaluate
annotate:
	python scripts/export_annotation_sample.py --input "$(IN)" --output "$(OUT)" --n 300
evaluate:
	python scripts/evaluate_manual_labels.py --input "$(IN)" --output "$(OUT)"
