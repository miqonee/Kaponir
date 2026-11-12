"""Structural text planning shared by experiments and the packaged worker.

All budgets come from the caller. A tokenizer callback decides whether a complete
request fits; no model names, language-specific words or characters/token ratios.
"""
import re
from collections import deque


def paragraph_spans(text):
    spans=[];start=0
    for match in re.finditer(r'\n[ \t]*\n',text):
        spans.append((start,match.end()));start=match.end()
    if start<len(text):spans.append((start,len(text)))
    return spans


def split_span(text,start,end):
    """Prefer paragraph, then line, then word boundaries nearest the middle."""
    middle=(start+end)//2
    for pattern in [r'\n[ \t]*\n',r'\n',r'\s+']:
        boundaries=[m.end()+start for m in re.finditer(pattern,text[start:end]) if start<m.end()+start<end]
        if boundaries:
            boundary=min(boundaries,key=lambda p:abs(p-middle))
            return (start,boundary),(boundary,end)
    if end-start<2:raise ValueError('model_fragment_cannot_split')
    return (start,middle),(middle,end)


def structural_context(text,offset):
    """Document preamble, active headings, table header, and previous paragraph.

This returns candidates; the caller trims them to its configured token budget.
"""
    preamble_end=re.search(r'^##+\s',text,re.M)
    preamble=text[:preamble_end.start()] if preamble_end else text[:(text.find('\n\n') if '\n\n' in text else text.find('\n') if '\n' in text else len(text))]
    headings=[];table=[];previous=''
    for line in text[:offset].splitlines():
        heading=re.match(r'^(#{1,6})\s+',line)
        if heading:
            level=len(heading[1]);headings=[h for h in headings if h[0]<level];headings.append((level,line))
        if '|' in line:
            if re.fullmatch(r'\s*\|?\s*:?-+.*',line) and '|' in previous:table=[previous,line]
        elif line.strip():table=[]
        previous=line
    before=text[:offset].rstrip()
    prior=before[before.rfind('\n\n')+2:] if '\n\n' in before else before
    return preamble, '\n'.join([h[1] for h in headings]+table), prior


def plan_text(text,fits,context_for=lambda offset:'',strategy='auto',max_fragments=1000):
    """Return disjoint source spans, preserving every character and structural boundaries."""
    if strategy not in ['auto','paragraph']:raise ValueError('invalid_chunk_strategy')
    pending=deque(paragraph_spans(text))
    output=[];current=None
    while pending:
        start,end=pending.popleft()
        context=context_for(current[0] if current else start)
        trial=(current[0],end) if current else (start,end)
        if fits(text[trial[0]:trial[1]],context):
            current=trial
            if strategy=='paragraph':output.append({'start':current[0],'end':current[1],'context':context});current=None
        elif current:
            output.append({'start':current[0],'end':current[1],'context':context_for(current[0])});current=None;pending.appendleft((start,end))
        else:
            left,right=split_span(text,start,end);pending.appendleft(right);pending.appendleft(left)
        if len(output)>max_fragments:raise ValueError('document_fragment_limit')
    if current:output.append({'start':current[0],'end':current[1],'context':context_for(current[0])})
    if len(output)>max_fragments:raise ValueError('document_fragment_limit')
    return output


def trim_context(text,limit,count_tokens,from_end=False):
    if not text or limit<=0:return ''
    if count_tokens(text)<=limit:return text
    lo,hi=0,len(text)
    while lo<hi:
        middle=(lo+hi+1)//2
        value=text[len(text)-middle:] if from_end else text[:middle]
        if count_tokens(value)<=limit:lo=middle
        else:hi=middle-1
    return text[len(text)-lo:] if from_end and lo else text[:lo]


def document_hint(text,offset,limit,count_tokens):
    if limit<=0:return ''
    root,structure,previous=structural_context(text,offset)
    # Equal shares are an explicit structural policy, never a tokenizer estimate.
    root_limit=limit//2
    root=trim_context(root,root_limit,count_tokens)
    structure=trim_context(structure,limit-root_limit,count_tokens)
    remaining=max(0,limit-count_tokens(root+'\n'+structure))
    previous=trim_context(previous,remaining,count_tokens,from_end=True)
    return trim_context('\n'.join(part for part in [root,structure,previous] if part),limit,count_tokens)
