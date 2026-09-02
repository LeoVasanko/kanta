# Fatal Error Handlers

Fatal background write errors can be observed with a decorator:

```python
import os
import signal

@kanta.fatal_error
async def on_fatal(err):
    os.kill(os.getpid(), signal.SIGTERM)  # Die
```

Multiple fatal handlers are supported and run in registration order.
