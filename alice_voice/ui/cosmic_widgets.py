"""Bounded, CPU-painted cosmic visualizations and accessible outline controls."""
import math
import random
from .vector_icons import renderer
from .cosmic_theme import PALETTE, VOLUME_HIDE_DELAY_MS, VOLUME_FADE_MS
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, QEvent, QPropertyAnimation
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QRadialGradient, QRegion, QLinearGradient, QPixmap
from PyQt6.QtWidgets import QWidget, QPushButton, QFrame, QHBoxLayout, QLabel, QSlider, QGraphicsOpacityEffect


class IconButton(QPushButton):
    def __init__(self, icon, name, callback=None, parent=None):
        super().__init__(parent)
        self.icon = icon
        self.active = False
        self.mode, self.phase = 'off', 0.0
        self.setFixedSize(48, 48)
        self.setAccessibleName(name)
        self.setToolTip(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if callback:
            self.clicked.connect(callback)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(self.width()/2, self.height()/2)
        scale = min(self.width(), self.height()) / 48
        p.scale(scale, scale)
        hovered = self.underMouse() and self.isEnabled()
        if hovered:
            glow = QRadialGradient(0, 0, 26)
            glow.setColorAt(0, QColor(50, 180, 255, 38))
            glow.setColorAt(1, QColor(50, 180, 255, 0))
            p.fillRect(QRectF(-26, -26, 52, 52), glow)
        if self.icon in ('mic', 'speaker', 'camera', 'more'):
            fill = QColor(PALETTE['accent'] if self.active else PALETTE['control'])
            p.setBrush(fill.lighter(125) if self.isDown() else fill.lighter(115) if hovered else fill)
            p.setPen(QPen(QColor('#6DDEFA' if self.active or hovered else PALETTE['control_border']), 1))
            p.drawEllipse(QPointF(0, 0), 22, 22)
        name = self.icon
        if name in ('mic', 'camera') and not self.active:
            name += '-off'
        color = '#041422' if self.active and self.icon in ('mic','camera') else '#51B5FF' if self.active else PALETTE['icon']
        # Glyphs retain the same optical size even in narrower navigation targets.
        p.resetTransform()
        key=(name,color)
        if getattr(self,'_icon_key',None)!=key:
            if hasattr(self,'_icon_renderer'): self._icon_renderer.deleteLater()
            self._icon_renderer=renderer(name,color,self)
            self._icon_key=key
        self._icon_renderer.render(p, QRectF((self.width()-24)/2, (self.height()-24)/2,24,24))
        if self.hasFocus() and self.property('keyboardFocus'):
            p.setPen(QPen(QColor('#51B5FF'),1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),8,8)


class CosmicBackground(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.phase, self.light, self.sidebar_width = 0., False, 290

    def advance(self, seconds):
        self.phase = (self.phase + min(.1,max(0,seconds))*.12) % math.tau
        self.update()

    def _build_scene(self, width, height):
        ratio=self.devicePixelRatioF()
        scene=QPixmap(math.ceil(width*ratio), math.ceil(height*ratio))
        scene.setDevicePixelRatio(ratio)
        scene.fill(QColor('#020611'))
        p=QPainter(scene)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        base=QLinearGradient(0,0,0,height)
        for at,color in ((0,'#020611'),(.6,'#040D20'),(.84,'#081A35'),(1,'#0B2140')):
            base.setColorAt(at,QColor(color))
        p.fillRect(QRectF(0,0,width,height),base)
        rng=random.Random(861)
        # Cached low-contrast dust at different scales; no grid or repeated dots.
        for i in range(32):
            x,y=rng.uniform(0,width),rng.uniform(.1,1.1)*height
            radius=rng.uniform(.09,.32)*width
            cloud=QRadialGradient(x,y,radius)
            cloud.setColorAt(0,QColor(15,43,82,rng.randrange(8,22)))
            cloud.setColorAt(1,QColor(5,15,35,0))
            p.fillRect(QRectF(0,0,width,height),cloud)
        for _ in range(2500):
            x,y=rng.uniform(0,width),rng.uniform(0,height)
            p.setPen(QColor(112,151,200,rng.randrange(2,8)))
            p.drawPoint(QPointF(x,y))
        for i in range(145):
            x,y=rng.uniform(0,width),rng.uniform(0,height)
            radius=rng.uniform(.3,.75) if i>9 else rng.uniform(.7,1.15)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(155,192,243,rng.randrange(25,90) if i>9 else 130))
            p.drawEllipse(QPointF(x,y),radius,radius)
        halo=QRadialGradient(width*.5,height*.37,min(width,height)*.3)
        halo.setColorAt(0,QColor(18,70,155,32)); halo.setColorAt(1,QColor(5,20,50,0))
        p.fillRect(QRectF(0,0,width,height),halo)
        vignette=QRadialGradient(width*.5,height*.45,max(width,height)*.72)
        vignette.setColorAt(.35,QColor(0,2,8,0)); vignette.setColorAt(1,QColor(0,2,8,95))
        p.fillRect(QRectF(0,0,width,height),vignette)
        # Soft elliptical blooms are cached with the stars at the actual DPR.
        # All light falls to zero within the bottom 15%; no moving cloud shapes.
        for x,rx,ry,alpha in ((.5,.78,.16,135),(0,.42,.145,175),(1,.42,.145,175)):
            p.save()
            p.translate(width*x,height*1.015)
            p.scale(max(1,width*rx),max(1,height*ry))
            bloom=QRadialGradient(0,0,1)
            bloom.setColorAt(0,QColor(20,105,255,alpha))
            bloom.setColorAt(.28,QColor(15,83,230,round(alpha*.72)))
            bloom.setColorAt(.65,QColor(9,54,170,round(alpha*.23)))
            bloom.setColorAt(1,QColor(6,30,100,0))
            p.fillRect(QRectF(-1,-1,2,2),bloom)
            p.restore()
        edge=QLinearGradient(0,height*.955,0,height)
        edge.setColorAt(0,QColor(20,115,255,0))
        edge.setColorAt(.65,QColor(25,130,255,16))
        edge.setColorAt(1,QColor(58,175,255,74))
        p.fillRect(QRectF(0,height*.955,width,height*.045),edge)
        p.end()
        return scene

    def paintEvent(self, event):
        left,w,h=self.sidebar_width,self.width()-self.sidebar_width,self.height()
        key=(w,h,self.devicePixelRatioF())
        if getattr(self,'_cache_key',None)!=key:
            self._scene=self._build_scene(max(1,w),h)
            self._cache_key=key
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(left,0,self._scene)
        p.fillRect(0,0,left,h,QColor(PALETTE['sidebar']))
        p.setPen(QColor('#23384F')); p.drawLine(left,0,left,h)


class CosmicOrb(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode, self.phase, self.level = 'off', 0., 0.
        self.fair_active = False
        self.setMinimumSize(140,140)
        self.setAccessibleName('Сфера состояния голосового помощника')

    def paintEvent(self,event):
        def glow(r,g,b,a):
            return QColor(255,120+g//4,42,a) if self.fair_active else QColor(r,g,b,a)
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(self.width()/2,self.height()/2)
        r=min(self.width(),self.height())*.36
        p.scale(r/100,r/100)
        dim = self.mode in ('off','paused','error','closing')
        target=PALETTE['orb_idle_opacity'] if dim else 1.
        self._brightness=getattr(self,'_brightness',target)+(target-getattr(self,'_brightness',target))*.16
        p.setOpacity(self._brightness)
        energy=self.level if self.mode in ('speaking','listening') else 0
        pulse=1+energy*.13
        p.scale(pulse,pulse)
        halo=QRadialGradient(0,0,135)
        halo.setColorAt(0,glow(18,100,255,95)); halo.setColorAt(.6,glow(18,80,255,55)); halo.setColorAt(1,glow(0,80,230,0))
        p.fillRect(QRectF(-138,-138,276,276),halo)
        body=QRadialGradient(-24,-24,122)
        body.setColorAt(0,glow(35,140,255,65)); body.setColorAt(.68,glow(9,36,106,100)); body.setColorAt(.88,glow(35,120,255,135)); body.setColorAt(1,glow(14,54,111,0))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(body); p.drawEllipse(QPointF(0,0),103,103)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Integrate speed instead of multiplying absolute time: a state change
        # must not teleport the ribbons to a different rotation.
        elapsed=min(.1,max(0.,self.phase-getattr(self,'_last_phase',self.phase)))
        self._last_phase=self.phase
        speed=.12 if dim else .26
        self._speed=getattr(self,'_speed',speed)+(speed-getattr(self,'_speed',speed))*.16
        self._ribbon_phase=getattr(self,'_ribbon_phase',.7)+elapsed*self._speed
        t=self._ribbon_phase
        for band in range(5):
            ribbon=QPainterPath()
            points=[]
            for edge in (-1,1):
                steps=range(121) if edge==-1 else range(120,-1,-1)
                for step in steps:
                    a=step*math.tau/120
                    lat=.45*math.sin(a*2+t+band*.86)+edge*.045
                    x=96*math.cos(a)*math.cos(lat)
                    y=96*(math.sin(a)*math.cos(lat)*math.cos(band*.46+t*.2)+math.sin(lat)*math.sin(band*.46+t*.2))
                    points.append(QPointF(x,y))
            ribbon.moveTo(points[0])
            for pt in points[1:]: ribbon.lineTo(pt)
            ribbon.closeSubpath()
            p.fillPath(ribbon,glow(35,110+band*9,255,22+band*4))
            path=QPainterPath()
            for step in range(121):
                a=step*math.tau/120
                # Three-dimensional spherical ribbons projected into the view.
                lat=.45*math.sin(a*2+t+band*.86)
                x=96*math.cos(a)*math.cos(lat)
                y=96*(math.sin(a)*math.cos(lat)*math.cos(band*.46+t*.2)+math.sin(lat)*math.sin(band*.46+t*.2))
                pt=QPointF(x,y)
                path.moveTo(pt) if step==0 else path.lineTo(pt)
            depth=.5+.5*abs(math.sin(band*.9+t*.3))
            for width,alpha,color in ((13,10,'#1264FF'),(7,24,'#1264FF'),(3.4,85,'#238CFF'),(1.3,215,'#65C5FF')):
                tint=QColor({'#1264FF':'#E96819','#238CFF':'#FF9D38','#65C5FF':'#FFD09A'}.get(color,color) if self.fair_active else color); tint.setAlpha(round(alpha*depth))
                p.setPen(QPen(tint,width*(.75+depth*.25))); p.drawPath(path)
        for i in range(3):
            p.save(); p.rotate(i*57+20+math.sin(t)*12)
            p.setPen(QPen(glow(52,121,255,60),.6)); p.drawEllipse(QRectF(-129,-37-i*5,258,74+i*10)); p.restore()
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(45):
            a=i*2.399+t*.2; distance=100+(i*17%33)
            p.setBrush(glow(117,214,255,55+i%4*35))
            p.drawEllipse(QPointF(math.cos(a)*distance,math.sin(a)*distance),.6+i%3*.22,.6+i%3*.22)


class AudioWave(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.level=0.; self.phase=0.; self.mode='off'
        self.setFixedHeight(56)
        self.setFixedWidth(350)
    def paintEvent(self,event):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(PALETTE['accent']),2,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
        for i in range(35):
            x=self.width()*(i+1)/36
            envelope=math.sin(math.pi*(i+1)/36)**2
            amplitude=self.level if self.mode in ('listening','speaking') else 0
            length=1+24*amplitude*envelope*(.35+.65*abs(math.sin(i*.72+self.phase*3)))
            p.drawLine(QPointF(x,self.height()/2-length),QPointF(x,self.height()/2+length))


class VolumeControl(QWidget):
    """One hover/focus region includes the popup and the speaker button."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setFixedSize(210,144)
        self.button=IconButton('speaker','Громкость озвучки',self.toggle,self)
        self.button.move(8,88)
        self.panel=QFrame(self); self.panel.setObjectName('volumePopup'); self.panel.setGeometry(0,0,210,54)
        row=QHBoxLayout(self.panel); row.setContentsMargins(12,8,12,8)
        self.slider=QSlider(Qt.Orientation.Horizontal); self.slider.setRange(0,100)
        self.slider.setAccessibleName('Громкость озвучки в процентах')
        self.label=QLabel(); self.label.setMinimumWidth(38)
        row.addWidget(self.slider,1); row.addWidget(self.label)
        self.keyboard_focus=False
        self.panel.hide()
        self.setMask(QRegion(self.button.geometry()))
        self.effect=QGraphicsOpacityEffect(self.panel); self.panel.setGraphicsEffect(self.effect)
        self.animation=QPropertyAnimation(self.effect,b'opacity',self); self.animation.setDuration(VOLUME_FADE_MS)
        self.animation.finished.connect(self._finish_animation)
        self.hide_timer=QTimer(self); self.hide_timer.setSingleShot(True); self.hide_timer.setInterval(VOLUME_HIDE_DELAY_MS)
        self.hide_timer.timeout.connect(self.hide_panel)
        self.slider.valueChanged.connect(lambda v:self.label.setText(f'{v}%'))
        self.slider.sliderPressed.connect(self.hide_timer.stop)
        self.slider.sliderReleased.connect(self._schedule_hide)
        for widget in (self,self.button,self.panel,self.slider): widget.installEventFilter(self)

    def set_compact(self, compact):
        self.button.move(8,48 if compact else 88)
        self.setFixedSize(210,104 if compact else 144)
        if self.panel.isVisible():
            self.setMask(QRegion(0,0,210,self.button.y()+2) | QRegion(self.button.geometry()))
        else:
            self.setMask(QRegion(self.button.geometry()))
        self.panel.setGeometry(0,0,210,44 if compact else 54)


    def _finish_animation(self):
        if self.effect.opacity()==0:
            self.panel.hide()
            self.setMask(QRegion(self.button.geometry()))

    def toggle(self):
        self.hide_panel(force=True) if self.panel.isVisible() else self.open_panel()

    def open_panel(self):
        self.setMask(QRegion(0,0,210,self.button.y()+2) | QRegion(self.button.geometry()))
        self.hide_timer.stop(); self.panel.show(); self.panel.raise_()
        self.animation.stop(); self.animation.setStartValue(self.effect.opacity()); self.animation.setEndValue(1.); self.animation.start()

    def _schedule_hide(self):
        if not self.slider.isSliderDown(): self.hide_timer.start()

    def hide_panel(self,force=False):
        if not force and (self.underMouse() or self.slider.isSliderDown() or self.keyboard_focus and (self.slider.hasFocus() or self.button.hasFocus())): return
        self.animation.stop(); self.animation.setStartValue(self.effect.opacity()); self.animation.setEndValue(0.); self.animation.start()

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Type.FocusIn:
            self.keyboard_focus=event.reason() != Qt.FocusReason.MouseFocusReason
            self.open_panel()
        elif event.type()==QEvent.Type.Enter: self.open_panel()
        elif event.type() in (QEvent.Type.Leave,QEvent.Type.FocusOut): self._schedule_hide()
        return super().eventFilter(watched,event)
