from clippyshot.converter import _page_sheet_names, _sheet_provenance

SHEETS=[{'name':'First','state':'visible','type':'worksheet'},
        {'name':'Hidden','state':'hidden','type':'worksheet'},
        {'name':'Last','state':'visible','type':'worksheet'}]

def test_single_page_export_keeps_hidden_sheet_in_page_order():
    assert _page_sheet_names(SHEETS,3,total_pages=3,single_page_sheets=True)==['First','Hidden','Last']
    coverage=_sheet_provenance(SHEETS,3,True,[{'index':1},{'index':2},{'index':3}],[])
    assert coverage['page_mapping_verified'] and coverage['rendered']==3
    assert coverage['non_rendered']==[] and coverage['inventory'][1]['state']=='hidden'
    assert coverage['inventory'][1]['page_number']==2


def test_ordinary_export_or_page_count_mismatch_does_not_guess_sheet_names():
    assert _page_sheet_names(SHEETS,3,total_pages=3,single_page_sheets=False)==[None]*3
    assert _page_sheet_names(SHEETS,2,total_pages=2,single_page_sheets=True)==[None]*2
    c=_sheet_provenance(SHEETS,2,True,[{'index':1},{'index':2}],[])
    assert not c['page_mapping_verified'] and c['rendered'] is None
    assert all(s['capture_status']=='unverified' for s in c['inventory'])


def test_blank_page_and_page_limit_are_distinct_from_hidden_sheet():
    c=_sheet_provenance(SHEETS,3,True,[{'index':1}],[2])
    assert [s['capture_status'] for s in c['inventory']]==['captured','blank_skipped','not_captured']
    assert [s['name'] for s in c['non_rendered']]==['Last']
