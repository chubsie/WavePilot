export const MIDDLE_HOLD_MS=500;

export class MiddleButton {
  constructor(onShort,onHold){this.onShort=onShort;this.onHold=onHold;this.down=false;this.startedAt=null;this.lastAt=null;this.holding=false;this.blocked=false;}
  cancel(){
    this.blocked=this.blocked||this.down;
    if(this.holding)this.onHold(false);
    this.down=false;this.holding=false;this.startedAt=null;this.lastAt=null;
  }
  update(down,now){
    if(this.lastAt!==null&&now-this.lastAt>500){this.cancel();this.blocked=this.blocked||down;}
    this.lastAt=now;
    if(this.blocked){if(!down)this.blocked=false;return;}
    if(down){
      if(!this.down)this.startedAt=now;
      this.down=true;
      if(!this.holding&&now-this.startedAt>=MIDDLE_HOLD_MS){this.holding=true;this.onHold(true);}
    }else if(this.down){
      if(this.holding)this.onHold(false);
      else if(now-this.startedAt<MIDDLE_HOLD_MS)this.onShort();
      this.down=false;this.holding=false;this.startedAt=null;
    }
  }
}
