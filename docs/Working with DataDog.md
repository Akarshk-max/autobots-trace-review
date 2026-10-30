# Working with DataDog

## Dashboards
Use dashboards to monitor a set of related metrics. [Dashboards Link to our datadog](https://app.datadoghq.com/dashboard/lists?p=1)
A panel in a dashboard can be set of filters and aggregation on the logs measures and dimensions. 

### Supported Dashboards
1. [Errors](https://app.datadoghq.com/dashboard/en2-igm-yzs/autobots-errors) 
2. [Usage](https://app.datadoghq.com/dashboard/jmk-6cu-x8s/autobots-usage)

Use Errors dashboard to monitor errors in deployments.

## Logs

Use Logs to:
- Explore detailed records of events, requests, and application behaviors.
- Identify and investigate errors and exceptions quickly.
- Set up alerts based on log patterns or specific error conditions.
- Analyze logs to detect trends or recurring issues.

### Log Views
Log queries can be saved for easy use next time.

#### Supported Log Views
- [Start Here](https://app.datadoghq.com/logs?query=-%40record.name%3Ahttpx._client%20-%40http.url%3A%22%2Fv1%2Fhello%2F%22%20-%22INFO%3A%22%20-%22API%20use%20complete%22%20-%22API%20use%20start%22%20-%22WARNING%3A%22%20-%22Error%20while%20parsing%20action%20doc%22%20-%22PydanticSerializationUnexpectedValue%22&additional_filters=%5B%5D&cols=service&fromUser=true&messageDisplay=inline&refresh_mode=sliding&saved-view-id=2897794&storage=hot&stream_sort=desc&viz=stream&from_ts=1747420174834&to_ts=1747506574834&live=true) Filters out unnecessary logs

### Get Started
- [Logs Explorer](https://docs.datadoghq.com/logs/explorer/)
- [Logs Search](https://docs.datadoghq.com/logs/explorer/search/)
- [Logs Search Syntax](https://docs.datadoghq.com/logs/explorer/search_syntax/)