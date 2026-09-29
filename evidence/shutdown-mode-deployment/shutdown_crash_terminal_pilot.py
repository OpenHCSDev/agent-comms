"""Actual terminal resize crash followed by mode removal during shutdown."""
from textual import work
from textual.geometry import Size
from textual.pilot import Pilot
from shutdown_mode_installed_pilot import ShutdownApp

class CrashShutdownApp(ShutdownApp):
 def on_mount(self):self.exercise()
 @work
 async def exercise(self):
  pilot=Pilot(self)
  await pilot.pause()
  await self.switch_mode('secondary')
  await pilot.pause()
  await self.switch_mode('first')
  await pilot.pause()
  await pilot.resize_terminal(139,25)
  await pilot.pause()
  self.call_after_refresh(self.crash_before_shutdown)
 def crash_before_shutdown(self):
  raise IndexError('CONTROLLED_PRE_SHUTDOWN_FAILURE')

if __name__=='__main__':
 app=CrashShutdownApp()
 app.run()
 assert isinstance(app._exception,IndexError),repr(app._exception)
 assert app.destroyed=={'content','secondary'},app.destroyed
 assert not app._registry and not app._installed_screens and not app._modes
 assert not any(app._screen_stacks.values())
 print('PASS original IndexError retained; actual terminal shutdown mode-removal completes all destruction')
