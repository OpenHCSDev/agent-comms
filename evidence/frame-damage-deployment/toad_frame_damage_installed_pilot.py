"""Actual terminal Toad frame shrink/growth and source-mode return, no prompts."""
import asyncio
import inspect
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from textual import work
from textual.pilot import Pilot
from agent_comms.comms import Comms
from toad.app import ToadApp
from toad.cli import run_terminal
from toad.widgets.agent_response import AgentResponse

class FrameToad(ToadApp):
 CSS_PATH=Path(inspect.getfile(ToadApp)).with_name('toad.tcss')
 async def on_mount(self,event):
  event.prevent_default()
  await super().on_mount()
  self.exercise()
 @work
 async def exercise(self):
  pilot=Pilot(self)
  await self.screen.wait_content_ready()
  first=self.current_mode
  response=await self.screen.conversation.post(AgentResponse('\n\n'.join(f'FRAME_PAINT_{i}' for i in range(50))))
  await pilot.pause()
  for width,height in ((139,25),(70,20),(139,40),(139,25)):
   await pilot.resize_terminal(width,height)
   await response.append('\n\nFRAME_TAIL_PAINTED')
   self.screen.conversation.window.scroll_end(animate=False,immediate=True)
   await pilot.pause()
   region=self.screen.conversation.window.content_region
   frame='\n'.join(s.crop(region.x,region.right).text for s in self.screen._compositor.render_strips()[region.y:region.bottom])
   assert 'FRAME_TAIL_PAINTED' in frame
  await self.new_session_screen(self.get_main_screen)
  await self.screen.wait_content_ready()
  await pilot.resize_terminal(139,25)
  await self.switch_mode(first)
  await self.screen.wait_content_ready()
  await pilot.pause()
  assert self._exception is None
  self.exit('ACTUAL_TERMINAL_TOAD_RESIZE_MODE_RETURN_PASS')

if __name__=='__main__':
 with TemporaryDirectory(dir='.artifacts',prefix='toad-frame-') as folder:
  root=Path(folder).resolve()
  os.environ.update(AGENT_COMMS_ROOT=str(root/'wire'),XDG_CONFIG_HOME=str(root/'config'),XDG_STATE_HOME=str(root/'state'),XDG_DATA_HOME=str(root/'data'))
  Comms(root/'wire').messaging.initialize_private_initial_protocol()
  app=FrameToad(project_dir=str(root))
  run_terminal(app)
  assert app.return_value=='ACTUAL_TERMINAL_TOAD_RESIZE_MODE_RETURN_PASS'
  print(app.return_value,flush=True)
