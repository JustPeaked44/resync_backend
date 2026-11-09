import io
import pytest
from docx import Document
from services.ingestion import extract_docx_with_metadata
from services.parser import ManuscriptParserService


@pytest.mark.parametrize('template', [None, ['Introduction', 'Research Methodology', 'Results']])
def test_contents_table_does_not_become_sections(template):
    doc = Document()
    table = doc.add_table(rows=0, cols=2)
    for title, page in [('CHAPTER I INTRODUCTION', ''), *[(f'Contents entry number {n}', str(n)) for n in range(1, 12)], ('Preface', 'iv'), ('CHAPTER III RESEARCH METHODOLOGY', ''), ('Design work', '31')]:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = title, page
    for heading in ['CHAPTER I INTRODUCTION', 'CHAPTER III RESEARCH METHODOLOGY', 'CHAPTER IV RESULTS']:
        doc.add_paragraph(heading)
        doc.add_paragraph('Real body text for the chapter. ' * 40)
    data = io.BytesIO()
    doc.save(data)
    text, records = extract_docx_with_metadata(data.getvalue())
    result = ManuscriptParserService.parse_manuscript_sections(text, template, records)
    sections = result['parsed_sections']
    assert not any('(2)' in name for name in sections)
    assert not any('Contents entry' in body or 'Preface | iv' in body or 'Design work | 31' in body for body in sections.values())
    assert sum('Real body text' in body for body in sections.values()) == 3


@pytest.mark.parametrize('template', [None, ['Introduction', 'Research Methodology', 'Results', 'Conclusion']])
def test_page_number_blocks_dot_leaders_and_digit_heading(template):
    text = '\n'.join(['Introduction', 'First topic 1', 'Second topic 2', 'Third topic 3', 'Old heading .... 4',
                      'CHAPTER 2 RESEARCH METHODOLOGY', 'Body text. ' * 50,
                      'CHAPTER 3 RESULTS', 'Results text. ' * 50, 'CHAPTER 4 CONCLUSION', 'Conclusion text. ' * 50])
    result = ManuscriptParserService.parse_manuscript_sections(text, template)
    bodies = '\n'.join(result['parsed_sections'].values())
    assert 'First topic 1' not in bodies
    assert 'Old heading' not in bodies
    assert 'Body text.' in bodies


def test_normal_document_keeps_heading_and_word_counts():
    text = '\n'.join(['CHAPTER 2 INTRODUCTION', 'Intro body words.', 'CHAPTER 3 RESEARCH METHODOLOGY', 'Method body words.'])
    result = ManuscriptParserService.parse_manuscript_sections(text, ['Introduction', 'Research Methodology'])
    assert {name: len(body.split()) for name, body in result['parsed_sections'].items()} == {
        'Introduction': 3, 'Research Methodology': 3}
