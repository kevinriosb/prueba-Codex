import json

import pytest

from backend.app.billing.extraction import DocumentExtractor
from .billing_fakes import TICKET,image,pdf
from .fake_model import ScriptedModel,answer
from .test_billing import billing_client


@pytest.mark.parametrize('mime,document,kind',[
    ('image/jpeg',image(),'input_image'),('application/pdf',pdf(),'input_file')])
def test_real_sdk_uses_separate_routing_and_portal_field_schemas_without_tools(billing_client,mime,document,kind):
    route={key:TICKET.get(key) for key in ('is_ticket','readable','merchant','billing_url','billing_url_evidence',
        'billing_email','billing_email_evidence','email_explicitly_for_billing')}
    model=ScriptedModel([[answer(json.dumps(route))],[answer(json.dumps(TICKET|{'store_number':'00042'}))]])
    client,app,*_=billing_client(model=model)
    extractor=DocumentExtractor(app.state.service,app.state.settings)
    first=client.portal.call(extractor.extract_routing,mime,document)
    assert first.merchant==TICKET['merchant'] and first.billing_url==TICKET['billing_url']
    assert first.folio is None and first.store_number is None and first.total is None
    second=client.portal.call(extractor.extract_fields,mime,document,[{'key':'store_number','label':'Número de tienda'}])
    assert second.store_number=='00042'
    assert len(model.inputs)==2 and all(call[0]['content'][1]['type']==kind for call in model.inputs)
    request=model.inputs[1][0]['content'][0]['text']
    assert 'store_number' in request and 'Número de tienda' in request
    assert 'name' not in request and 'postal_code' not in request
