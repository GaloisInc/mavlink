goog.provide('Mavelous.PFD');

Mavelous.PFD = function(origin, max_x, max_y) {
  var art_horiz = new Mavelous.PFD.ArtificialHorizon(origin.add([125,5]));

  var whole_canvas = new paper.Path([[0,0],[0,max_y],[max_x,max_y],[max_x,0]]);
  whole_canvas.closed = true;
  var bleed = whole_canvas.subtract(art_horiz.boundingBox);
  bleed.fillColor = new paper.Color(0.3,0.3,0.3,1.0); //0.5); // alpha 0.5 for debugging

  var air_ind = new Mavelous.PFD.AirspeedIndicator(origin.add([25,100]));
  var alt_ind = new Mavelous.PFD.AltitudeIndicator(origin.add([350,100]));
  var head_ind = new Mavelous.PFD.HeadingIndicator(origin.add([250,230]));

  return {
    airspeed : air_ind,
    altitude : alt_ind,
    heading  : head_ind,
    horizon  : art_horiz,
    draw     : function () { paper.view.draw() } // XXX
  };
};

Mavelous.PFD.AirspeedIndicator = function(origin) {
  var boundary = new paper.Path();
  boundary.strokeWidth = 5;
  boundary.strokeColor = 'white';

  boundary.moveTo(origin);
  boundary.lineTo(origin.add([100, 0]));
  boundary.lineTo(origin.add([125, 25]));
  boundary.lineTo(origin.add([100, 50]));
  boundary.lineTo(origin.add([  0, 50]));
  boundary.closed = true;
  boundary.fillColor = 'black';

  var lbl = new paper.PointText(origin.add([5,65]));
  lbl.content = 'AIRSPEED (M/S)';

  var indicator = new paper.PointText(origin.add([25, 33]));
  indicator.fontSize = 24;
  indicator.fillColor = 'white';
  indicator.content = '99';

  var target = new paper.PointText(origin.add([75, 33]));
  target.fontSize = 24;
  target.fillColor = 'red';
  target.content = '88';


  return {
    setIndicated : function (value) {
      indicator.content = value;
    },
    setTarget    : function (value) {
      target.content = value;
    }
  };
};

Mavelous.PFD.AltitudeIndicator = function(origin) {
  var boundary = new paper.Path();
  boundary.strokeWidth = 5;
  boundary.strokeColor = 'white';

  boundary.moveTo(origin.add([125,0]));
  boundary.lineTo(origin.add([25, 0]));
  boundary.lineTo(origin.add([0,  25]));
  boundary.lineTo(origin.add([25, 50]));
  boundary.lineTo(origin.add([125,50]));
  boundary.closed = true;
  boundary.fillColor = 'black'

  var lbl = new paper.PointText(origin.add([35,65]));
  lbl.content = 'ALTITUDE (M)';

  var indicator = new paper.PointText(origin.add([75, 33]));
  indicator.fontSize = 24;
  indicator.fillColor = 'white';
  indicator.content = '99';

  var target = new paper.PointText(origin.add([25, 33]));
  target.fontSize = 24;
  target.fillColor = 'red';
  target.content = '88';


  return {
    setIndicated : function (value) {
      indicator.content = value;
    },
    setTarget    : function (value) {
      target.content = value;
    }
  };
};

Mavelous.PFD.HeadingIndicator = function(origin) {
  var boundary = new paper.Path();
  boundary.strokeWidth = 5;
  boundary.strokeColor = 'white';

  boundary.moveTo(origin);
  boundary.lineTo(origin.add([75, 25]));
  boundary.lineTo(origin.add([75, 75]));
  boundary.lineTo(origin.add([-75, 75]));
  boundary.lineTo(origin.add([-75,25]));
  boundary.closed = true;
  boundary.fillColor = 'black';


  var lbl = new paper.PointText(origin.add([-25,90]));
  lbl.content = 'HEADING';

  var indicator = new paper.PointText(origin.add([-20, 65]));
  indicator.fontSize = 24;
  indicator.fillColor = 'white';
  indicator.content = '99';

  var target = new paper.PointText(origin.add([-20, 35]));
  target.fontSize = 24;
  target.fillColor = 'red';
  target.content = '88';


  return {
    setIndicated : function (value) {
      indicator.content = value;
    },
    setTarget    : function (value) {
      target.content = value;
    }
  };
};

Mavelous.PFD.ArtificialHorizon = function(origin) {
  var x_dim = 250;
  var y_dim = 250;
  var radius = Math.min(x_dim,y_dim) / 2.0;
  var skyColor = '#72cde4';
  var groundColor = '#c0a020';

  var center_x = origin.x + (x_dim / 2.0)
  var center_y = origin.y + (y_dim / 2.0)

  var ground = new paper.Path([[center_x-x_dim, center_y],
                        [center_x-x_dim, center_y+y_dim],
                        [center_x+x_dim, center_y+y_dim],
                      [center_x+x_dim, center_y]]);
  ground.closed = true;
  ground.fillColor = groundColor;

  var sky = new paper.Path([[center_x-x_dim, center_y],
                        [center_x-x_dim, center_y-y_dim],
                        [center_x+x_dim, center_y-y_dim],
                      [center_x+x_dim, center_y]]);
  sky.closed = true;
  sky.fillColor = skyColor

  var horizon = new paper.Path();
  horizon.moveTo(new paper.Point(center_x - x_dim, center_y));
  horizon.lineTo(new paper.Point(center_x + x_dim, center_y));
  horizon.strokeWidth = 5;
  horizon.strokeColor = '#343434';

  var ladder = function (height, width) {
    var rung = new paper.Path([[center_x-(width/2),center_y-height],
      [center_x+(width/2),center_y-height]]);
    rung.strokeWidth = 2;
    rung.strokeColor = '#202020'
    return rung;
  };

  var rungs = [ new ladder(15,25), new ladder(30,35),
                new ladder(45,25), new ladder(60,45),
                new ladder(-15,25), new ladder(-30,35),
                new ladder(-45,25), new ladder(-60,45) ];


  var moving_group = new paper.Group([ ground,sky,horizon ]);
  moving_group.addChildren(rungs)

  var h_pitch = 0;
  var h_roll = 0;

  var setpitchroll = function (pitch_rads,roll_rads) {
    var pitch = pitch_rads * (180 / Math.PI);
    var roll = roll_rads * (180 / Math.PI);
    moving_group.rotate(-h_roll,[center_x,center_y]);
    moving_group.translate(0,-h_pitch);
    moving_group.translate(0,pitch);
    moving_group.rotate(roll,[center_x,center_y]);
    h_pitch = pitch;
    h_roll  = roll;
  };

  var indicator_left = new paper.Path([[center_x - 60, center_y],
    [center_x - 30, center_y],
    [center_x - 20, center_y + 10]]);
  indicator_left.strokeWidth = 5;
  indicator_left.strokeColor = 'black';
  var indicator_right = new paper.Path([[center_x + 60, center_y],
    [center_x + 30, center_y],
    [center_x + 20, center_y + 10]]);
  indicator_right.strokeWidth = 5;
  indicator_right.strokeColor = 'black';

  var boundingbox = new paper.Path();
  boundingbox.strokeWidth = 5;
  boundingbox.strokeColor = 'black';

  boundingbox.moveTo(origin);
  boundingbox.lineTo(origin.add([x_dim, 0]));
  boundingbox.lineTo(origin.add([x_dim, y_dim]));
  boundingbox.lineTo(origin.add([    0, y_dim]));
  boundingbox.closed = true;

  return {
    boundingBox : boundingbox,
    setPitchRoll: setpitchroll
  };
};

