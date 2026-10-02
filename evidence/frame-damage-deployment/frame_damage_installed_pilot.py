import asyncio
from textual.app import App
from textual.widgets import Static
from textual.geometry import Size

class FrameApp(App):
 def compose(self):yield Static('\n'.join(f'FRAME_CONTENT_{i}' for i in range(40)))

async def main():
 app=FrameApp()
 async with app.run_test(size=(139,40)) as pilot:
  await pilot.pause()
  compositor=app.screen._compositor
  # Native reflow keeps old scene damage while admitting the new frame size.
  compositor.reflow(app.screen,Size(139,25))
  update=compositor.render_partial_update()
  assert update is not None
  print('FRAME',compositor.size,'CHOPS',len(update.chops),'LAST_SPAN',update.spans[-1],flush=True)
  sequence=update.render_segments(app.console)
  assert 'FRAME_CONTENT' in sequence
  print('PASS actual native scene resize damage rendered',flush=True)
asyncio.run(main())
