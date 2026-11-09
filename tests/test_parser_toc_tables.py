import io
import pytest
from docx import Document
from services.ingestion import extract_docx_with_metadata
from services.parser import ManuscriptParserService


@pytest.mark.parametrize('template', [None, ['Introduction', 'Research Methodology', 'Results']])
def test_contents_table_does_not_become_sections(template):
    doc = Document()
    table = doc.add_table(rows=0, cols=2)
    for title, page in [('CHAPTER I INTRODUCTION', ''), *[(f'Contents entry number {n}', str(n)) for n in range(1, 12)], ('CHAPTER III RESEARCH METHODOLOGY', ''), *[(f'Design work {n}', str(n)) for n in range(31, 36)]]:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = title, page
    for heading in ['CHAPTER I INTRODUCTION', 'CHAPTER III RESEARCH METHODOLOGY', 'CHAPTER IV RESULTS']:
        doc.add_paragraph(heading)
        doc.add_paragraph('Real body text for the chapter. ' * 40)
        body_table = doc.add_table(rows=0, cols=2)
        for label, value in [('Evidence', '42'), ('Finding', '43'), ('Measure', '44')]:
            cells = body_table.add_row().cells
            cells[0].text, cells[1].text = label, value
    data = io.BytesIO()
    doc.save(data)
    text, records = extract_docx_with_metadata(data.getvalue())
    result = ManuscriptParserService.parse_manuscript_sections(text, template, records)
    sections = result['parsed_sections']
    assert not any('(2)' in name for name in sections)
    assert not any('Contents entry' in body or 'Design work 31 | 31' in body for body in sections.values())
    assert sum('Real body text' in body for body in sections.values()) == 3
    assert sum('Evidence | 42' in body and 'Measure | 44' in body for body in sections.values()) == 3


@pytest.mark.parametrize('template', [None, ['Introduction', 'Research Methodology', 'Results', 'Conclusion']])
def test_page_number_blocks_dot_leaders_and_digit_heading(template):
    text = '\n'.join(['Introduction', 'First topic 1', 'Second topic 2', 'Third topic 3', 'Old heading .... 4',
                      'CHAPTER 2 RESEARCH METHODOLOGY', 'Body text. ' * 50,
                      'CHAPTER 3 RESULTS', 'Results text. ' * 50, 'CHAPTER 4 CONCLUSION', 'Conclusion text. ' * 50])
    result = ManuscriptParserService.parse_manuscript_sections(text, template)
    bodies = '\n'.join(result['parsed_sections'].values())
    assert 'Body text.' in bodies


def test_page_runs_require_five_nondecreasing_entries():
    lines = ['Introduction', 'Body words.'] + [f'Keep {n} | {n}' for n in range(1, 5)] + ['Body break.'] + [f'Drop {n} | {n}' for n in range(1, 6)] + ['Preface | iv', 'Preface | v', 'Preface | vi', 'Preface | vii', 'Preface | viii', 'Body break.', 'Evidence | 42']
    result = ManuscriptParserService.parse_manuscript_sections('\n'.join(lines), ['Introduction'])
    body = result['parsed_sections']['Introduction']
    assert all(f'Keep {n} | {n}' in body for n in range(1, 5))
    assert all(f'Drop {n} | {n}' not in body for n in range(1, 6))
    assert 'Preface | viii' not in body
    assert 'Evidence | 42' in body


def test_descending_large_and_numeric_labels_do_not_form_a_run():
    rows = ['Item | 5', 'Item | 4', 'Item | 3', 'Item | 2', 'Item | 1',
            'Label | 1000', '1 | 1', '2 | 2', '3 | 3', '4 | 4', '5 | 5']
    result = ManuscriptParserService.parse_manuscript_sections('\n'.join(['Introduction', *rows]), ['Introduction'])
    assert all(row in result['parsed_sections']['Introduction'] for row in rows)


def test_normal_document_keeps_heading_and_word_counts():
    text = '\n'.join(['CHAPTER 2 INTRODUCTION', 'Intro body words.', 'CHAPTER 3 RESEARCH METHODOLOGY', 'Method body words.'])
    result = ManuscriptParserService.parse_manuscript_sections(text, ['Introduction', 'Research Methodology'])
    assert {name: len(body.split()) for name, body in result['parsed_sections'].items()} == {
        'Introduction': 3, 'Research Methodology': 3}
