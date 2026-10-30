# Code Structure

Each folder in Autobots represent a Service  
(Service here refers to distinct feature of Autobots)

Eg. Folder x
> ls ./x  
|-> x_doc_model.py (mongodb and pydantic model definitions)  
|-> x_crud.py (DB operations)
|-> x.py (functionality)  
|-> user_x.py (multi-tenant wrapper over functionality) (Currently acting as Service layer)
|-> x_service.py (contract x provides to the world)  

Folder y
> ./y => should use only x_service.py from folder x  


Common:  
- ./core
- ./conn
- ./api