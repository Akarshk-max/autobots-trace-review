# Actions

## Action Run general directions

### LLM actions

LLM actions, like all actions, will have a config and accept input on run.
The input object is DataBlock (as of now). DataBlock has at least 2 important fields.
1. text - string data
2. urls - list of file urls (file url can be any file type like pdf, image, audio, video)

When writing run method for LLM actions, you should do all the following:

 - Prepare input message(s) + config for LLM.
  - Add input text to the input message.
  - Upload files to the LLM provider. (store link mapping in cache). Then add provider file objects to the input message.
  - Expand provider-related tools to replace the tool name with tool definition
  - Expand functional tools to replace tool name with tool definition

 - Call the LLM provider and get a response from the LLM provider

 - Process the response
  - if a file has been created in response, then Upload a file to S3 and replace the file url in the response.
  - append the response to the config input messages
  - process the response - call tool on tool call response
  - keep the loop until the finish reason is stop
