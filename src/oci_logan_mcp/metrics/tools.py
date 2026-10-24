"""Compact public schemas; detailed examples live in Monitoring resources."""


def get_metric_tools() -> list[dict]:
    text={'type':'string'}
    filters={'type':'object','additionalProperties':{'type':'string'}}
    scope={'compartment_id':text,'region':text,
        'include_subcompartments':{'type':'boolean','default':False}}
    paging={'limit':{'type':'integer','minimum':1,'maximum':100,'default':50},'page_token':text}
    bounds={'time_start':text,'time_end':text,'time_range':text,
        'resolution':{'type':'string','enum':['1m','5m','1h','1d']},
        'max_series':{'type':'integer','minimum':1,'maximum':100,'default':20},
        'max_datapoints':{'type':'integer','minimum':1,'maximum':10000,'default':4000}}
    def tool(name,description,properties,required):
        return {'name':name,'description':description,
            'inputSchema':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}
    return [
        tool('list_metric_namespaces','Discover one page of visible Monitoring namespaces in the chosen scope.',
            {**scope,**paging},[]),
        tool('list_metrics','Discover one page of metric streams and dimensions. Follow next_page_token.',
            {**scope,**paging,'namespace':text,'metric_name':text,'resource_group':text,'dimension_filters':filters},['namespace']),
        tool('search_metric_catalog','Search reviewed metric keys and aliases locally. Resolve ambiguous candidates before querying.',
            {'query':text,'namespace':text,'resource_group':text,
             'limit':{'type':'integer','minimum':1,'maximum':100,'default':25}},['query']),
        tool('query_catalog_metric','Query an exact reviewed metric key. Resolution follows interval. See monitoring://query-templates.',
            {**scope,**bounds,'metric_key':text,'aggregation':{'type':'string','enum':['mean','max','min','sum']},
             'interval':{'type':'string','enum':['1m','5m','1h','1d']},'dimension_filters':filters,
             'group_by':{'type':'array','items':text}},['metric_key']),
        tool('query_metrics','Run bounded raw MQL. Omitted resolution follows query intervals. See monitoring://syntax-guide.',
            {**scope,**bounds,'namespace':text,'query':{'type':'string','maxLength':4096},'resource_group':text},['namespace','query']),
    ]
