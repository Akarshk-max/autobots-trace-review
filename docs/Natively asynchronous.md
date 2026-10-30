# Natively asynchronous
API should return immediately with partial result. 
Partial result is used to poll or notify for complete result.

## Thoughts
## Implementation
To implement Natively asynchronous feature. 
Corresponding feature_crud takes in feature specific BaseModels. 
We will add a new and the only mandatory field in Doc models. 
Field name `uid` and Value will be `f"{x}k_{_id}`. 
Eg. Value for uid in ActionDoc will be `ak_{_id}`. (Seen in Slack api models)
