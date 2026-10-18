"""Exercise installed Toad construction/probing and its actual terminal driver."""
import asyncio, fcntl, importlib.metadata, json, os, pathlib, struct, sys, termios

def main():
    stage=pathlib.Path(sys.argv[1])
    before=struct.unpack('HHHH',fcntl.ioctl(sys.stdout.fileno(),termios.TIOCGWINSZ,bytes(8)))
    assert before[1] == 0, before
    # No application, probe, driver, widget or I/O function is substituted.
    from toad.app import ToadApp
    assert 'textual_image._terminal' not in sys.modules, 'probe ran before the application startup boundary'
    app=ToadApp(project_dir=str(stage/'project'))
    from textual_image._terminal import probe_terminal
    capabilities=probe_terminal()
    expected=json.loads((stage/'expected.json').read_text())
    assert list(capabilities.cell_size)==expected['cell'], capabilities
    assert capabilities.sixel, capabilities
    async def inspect_first_frame(pilot):
        await pilot.pause()
        text='\n'.join(strip.text for strip in app.screen._compositor.render_strips())
        assert text.strip(), 'application did not render a first frame'
        (stage/'frame.txt').write_text(text)
        app.exit()
    app.run(auto_pilot=inspect_first_frame)
    if app._exception is not None:
        raise app._exception
    result={'before_ioctl':before,'cell_size':capabilities.cell_size,'sixel':capabilities.sixel,'first_frame':True,'zero_division':0,'probe_file':sys.modules['textual_image._terminal'].__file__,'textual_image':json.loads(importlib.metadata.distribution('textual-image').read_text('direct_url.json')),'runtime':str(pathlib.Path(sys.executable).parent.parent)}
    (stage/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')

if __name__=='__main__':
    main()
