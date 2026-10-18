pip install -r requirements.txt
pip install pytest pytest-asyncio
python3 -m spacy download en_core_web_sm
python3 -m pytest tests/
