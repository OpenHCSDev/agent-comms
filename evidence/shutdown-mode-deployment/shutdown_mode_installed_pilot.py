"""Actual async screen teardown may remove another mode while shutdown awaits it."""
import asyncio
from textual.app import App
from textual.screen import Screen
from textual.widgets import Static

class RemovalContent(Static):
 def on_unmount(self):
  self.app.remove_mode('secondary')
  self.app.destroyed.add('content')

class SecondaryScreen(Screen):
 def compose(self):yield Static('SECONDARY')
 def on_unmount(self):self.app.destroyed.add('secondary')

class FirstScreen(Screen):
 def compose(self):yield RemovalContent('SHUTDOWN_PAINT')

class ShutdownApp(App):
 DEFAULT_MODE='first'
 MODES={'first':FirstScreen,'secondary':SecondaryScreen}
 def __init__(self):
  super().__init__();self.destroyed=set()

async def main():
 app=ShutdownApp()
 async with app.run_test(size=(139,40)) as pilot:
  await pilot.pause()
  first=app.current_mode
  await app.switch_mode('secondary')
  await pilot.pause()
  await app.switch_mode(first)
  await pilot.resize_terminal(139,25)
  await pilot.pause()
 assert app.destroyed=={'content','secondary'},app.destroyed
 assert not app._registry
 assert not app._installed_screens and not app._modes
 assert not any(app._screen_stacks.values())
 print('PASS actual resize/mode-return/shutdown: remove_mode during awaited unmount, all pumps/resources retired')

if __name__=='__main__':asyncio.run(main())
